"""Photos and documents attached to records.

Files are copied into the attachment store (``<data>/attachments/YYYY/MM/``)
under a random name, so the original can be moved or deleted from the
Desktop without breaking the link. The SHA-256 hash detects the same scan
being attached twice.
"""

from __future__ import annotations

import hashlib
import mimetypes
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from wms.core.enums import AttachmentKind
from wms.core.exceptions import NotFoundError, ValidationError
from wms.core.numbers import utcnow
from wms.core.permissions import P
from wms.db.models import Attachment
from wms.services import audit
from wms.services.base import BaseService

ALLOWED = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".bmp": "image/bmp",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".pdf": "application/pdf",
}
PHOTO_EXT = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp"}

# First bytes of each allowed format. A file renamed to .pdf (or a damaged download) is refused
# instead of being stored as a "delivery note" nobody can open later.
SIGNATURES = {
    "image/jpeg": (b"\xff\xd8\xff",),
    "image/png": (b"\x89PNG\r\n\x1a\n",),
    "image/bmp": (b"BM",),
    "image/gif": (b"GIF87a", b"GIF89a"),
    "application/pdf": (b"%PDF-",),
}


def content_matches(path: Path, mime: str) -> bool:
    with open(path, "rb") as f:
        head = f.read(16)
    if mime == "image/webp":
        return head[:4] == b"RIFF" and head[8:12] == b"WEBP"
    if mime == "application/pdf":
        # Some scanners write a few bytes (BOM, whitespace) before the PDF header.
        with open(path, "rb") as f:
            return b"%PDF-" in f.read(1024)
    return any(head.startswith(sig) for sig in SIGNATURES.get(mime, (b"",)))


@dataclass
class StagedFile:
    """A file already copied into the store, waiting for its database row."""

    source: Path
    rel: str
    dest: Path
    kind: str
    mime: str
    size: int
    sha256: str


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


class AttachmentService(BaseService):
    @property
    def store(self) -> Path:
        return self.ctx.config.attachments_path

    def path_of(self, att: Attachment) -> Path:
        return self.store / att.stored_name

    def list_for(self, entity_type: str, entity_id: int, kind: str | None = None) -> list[Attachment]:
        with self.db.read_session() as s:
            q = (
                select(Attachment)
                .where(Attachment.entity_type == entity_type, Attachment.entity_id == entity_id,
                       Attachment.is_deleted.is_(False))
                .order_by(Attachment.is_primary.desc(), Attachment.uploaded_at)
            )
            if kind:
                q = q.where(Attachment.kind == kind)
            return list(s.scalars(q))

    def primary_photo(self, entity_type: str, entity_id: int) -> Attachment | None:
        photos = self.list_for(entity_type, entity_id, AttachmentKind.PHOTO.value)
        return photos[0] if photos else None

    def find_duplicates(self, file_path: str | Path) -> list[Attachment]:
        digest = sha256_of(Path(file_path))
        with self.db.read_session() as s:
            return list(s.scalars(select(Attachment).where(Attachment.sha256 == digest,
                                                           Attachment.is_deleted.is_(False))))

    def validate_file(self, file_path: str | Path) -> Path:
        src = Path(file_path)
        if not src.is_file():
            raise ValidationError(f"File not found: {src}")
        ext = src.suffix.lower()
        if ext not in ALLOWED:
            raise ValidationError("Only JPG, PNG, BMP, GIF, WEBP and PDF files can be attached.")
        size = src.stat().st_size
        max_bytes = int(self.ctx.config.max_attachment_mb) * 1024 * 1024
        if size > max_bytes:
            raise ValidationError(f"File is larger than {self.ctx.config.max_attachment_mb} MB.")
        if size == 0:
            raise ValidationError("File is empty.")
        try:
            ok = content_matches(src, ALLOWED[ext])
        except OSError as exc:
            raise ValidationError(f"File cannot be read: {exc}") from None
        if not ok:
            raise ValidationError(f"'{src.name}' is not a valid {ext[1:].upper()} file (it may be damaged or "
                                  f"renamed). Save or scan it again and attach the new file.")
        return src

    # Two-step attach: ``stage`` copies the file into the store (outside any transaction);
    # ``attach_staged`` writes the row inside the caller's transaction. If that transaction
    # rolls back, ``discard`` removes the copies, so no record ever points at a missing file
    # and no document is saved without the scans the user attached.
    def stage(self, file_path: str | Path, kind: str | None = None) -> StagedFile:
        src = self.validate_file(file_path)
        ext = src.suffix.lower()
        now = utcnow()
        rel = Path(f"{now:%Y}") / f"{now:%m}" / f"{uuid.uuid4().hex}{ext}"
        dest = self.store / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        try:
            digest, size = sha256_of(dest), dest.stat().st_size
            if size != src.stat().st_size:
                raise ValidationError(f"'{src.name}' was not copied completely - try again.")
        except Exception:
            dest.unlink(missing_ok=True)
            raise
        return StagedFile(src, rel.as_posix(), dest,
                          kind or (AttachmentKind.PHOTO.value if ext in PHOTO_EXT else AttachmentKind.DOCUMENT.value),
                          ALLOWED.get(ext) or mimetypes.guess_type(src.name)[0] or "application/octet-stream",
                          size, digest)

    @staticmethod
    def discard(staged: list[StagedFile]) -> None:
        for f in staged:
            f.dest.unlink(missing_ok=True)

    def attach_staged(self, s: Session, entity_type: str, entity_id: int, f: StagedFile,
                      caption: str | None = None, make_primary: bool = False) -> Attachment:
        existing = list(s.scalars(select(Attachment).where(
            Attachment.entity_type == entity_type, Attachment.entity_id == entity_id,
            Attachment.kind == f.kind, Attachment.is_deleted.is_(False))))
        is_primary = make_primary or (f.kind == AttachmentKind.PHOTO.value and not existing)
        if is_primary:
            for e in existing:
                e.is_primary = False
        att = Attachment(entity_type=entity_type, entity_id=entity_id, kind=f.kind,
                         original_name=f.source.name[:260], stored_name=f.rel, mime_type=f.mime, size_bytes=f.size,
                         sha256=f.sha256, is_primary=is_primary, caption=caption, uploaded_by_id=self.user.id)
        s.add(att)
        s.flush()
        audit.record(s, self.ctx, "ATTACHMENT_ADDED", entity_type, entity_id, f.source.name,
                     f"{AttachmentKind(f.kind).label} '{f.source.name}' attached as {f.rel}.")
        return att

    def add(self, entity_type: str, entity_id: int, file_path: str | Path, kind: str | None = None,
            caption: str | None = None, make_primary: bool = False) -> Attachment:
        self.require(P.ATTACHMENTS_ADD)
        staged = self.stage(file_path, kind)
        try:
            with self.db.session() as s:
                return self.attach_staged(s, entity_type, entity_id, staged, caption, make_primary)
        except BaseException:
            self.discard([staged])
            raise

    def replace_photo(self, attachment_id: int, file_path: str | Path) -> Attachment:
        """Replace a photo: the old one is kept (hidden) for the audit trail."""
        with self.db.read_session() as s:
            old = s.get(Attachment, attachment_id)
            if old is None:
                raise NotFoundError("Photo not found.")
            entity_type, entity_id, was_primary = old.entity_type, old.entity_id, old.is_primary
        new = self.add(entity_type, entity_id, file_path, AttachmentKind.PHOTO.value, make_primary=was_primary)
        self.remove(attachment_id, reason="Replaced", _skip_perm=True)
        return new

    def set_primary(self, attachment_id: int) -> None:
        self.require(P.ATTACHMENTS_ADD)
        with self.db.session() as s:
            att = s.get(Attachment, attachment_id)
            if att is None:
                raise NotFoundError("Attachment not found.")
            for other in s.scalars(select(Attachment).where(Attachment.entity_type == att.entity_type,
                                                            Attachment.entity_id == att.entity_id,
                                                            Attachment.kind == att.kind)):
                other.is_primary = other.id == att.id

    def remove(self, attachment_id: int, reason: str | None = None, _skip_perm: bool = False) -> None:
        """Soft delete: the file stays on disk and the record stays in the database."""
        if not _skip_perm:
            self.require(P.ATTACHMENTS_DELETE)
        with self.db.session() as s:
            att = s.get(Attachment, attachment_id)
            if att is None:
                raise NotFoundError("Attachment not found.")
            att.is_deleted = True
            att.is_primary = False
            att.deleted_at = utcnow()
            att.deleted_by_id = self.user.id
            audit.record(s, self.ctx, "ATTACHMENT_REMOVED", att.entity_type, att.entity_id, att.original_name,
                         f"'{att.original_name}' removed. {reason or ''}".strip())
            s.flush()
            if att.kind == AttachmentKind.PHOTO.value:
                remaining = s.scalars(select(Attachment).where(
                    Attachment.entity_type == att.entity_type, Attachment.entity_id == att.entity_id,
                    Attachment.kind == att.kind, Attachment.is_deleted.is_(False)).order_by(Attachment.uploaded_at))
                first = next(iter(remaining), None)
                if first is not None and not any(
                        a.is_primary for a in s.scalars(select(Attachment).where(
                            Attachment.entity_type == att.entity_type, Attachment.entity_id == att.entity_id,
                            Attachment.is_deleted.is_(False), Attachment.is_primary.is_(True)))):
                    first.is_primary = True

    def missing_files(self) -> list[Attachment]:
        with self.db.read_session() as s:
            return [a for a in s.scalars(select(Attachment).where(Attachment.is_deleted.is_(False)))
                    if not self.path_of(a).is_file()]
