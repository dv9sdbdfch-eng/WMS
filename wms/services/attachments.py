"""Photos and documents attached to records.

Files are copied into the attachment store (``<data>/attachments/YYYY/MM/``)
under a random name, so the original can be moved or deleted from the
Desktop without breaking the link. The SHA-256 hash detects the same scan
being attached twice.

On the shared server (PostgreSQL) the content is also stored in the database
(``attachment_files``, once per distinct file), so PCs in other buildings see
the same photos and scans without a network share. The attachments folder of
each PC then works as a cache: ``path_of`` downloads a file the first time it
is opened on that PC.
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
from wms.db.models import Attachment, AttachmentFile
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


THUMB_PX = 256


def make_thumbnail(path: Path) -> bytes | None:
    """A small JPEG of a photo for lists, or None (not an image / cannot be read)."""
    try:
        from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QSize, Qt
        from PySide6.QtGui import QImageReader
    except ImportError:  # pragma: no cover
        return None
    reader = QImageReader(str(path))
    if not reader.canRead():
        return None
    size = reader.size()
    if size.isValid() and size.width() > 0 and size.height() > 0:
        reader.setScaledSize(size.scaled(QSize(THUMB_PX, THUMB_PX), Qt.AspectRatioMode.KeepAspectRatio))
    img = reader.read()
    if img.isNull():
        return None
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    if not img.save(buf, "JPEG", 82):
        return None
    return bytes(ba.data())


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

    @property
    def shared(self) -> bool:
        """Content kept in the database too (shared server), not only in this PC's folder."""
        return self.db.dialect == "postgresql"

    def path_of(self, att: Attachment) -> Path:
        """The file on this PC; on the shared server it is downloaded first if this PC has no copy."""
        path = self.store / att.stored_name
        if not path.is_file() and self.shared:
            self._download(att.sha256, path)
        return path

    def _download(self, sha256: str, path: Path) -> bool:
        with self.db.read_session() as s:
            data = s.scalar(select(AttachmentFile.data).where(AttachmentFile.sha256 == sha256))
        if data is None or hashlib.sha256(data).hexdigest() != sha256:
            return False
        _write_atomic(path, data)
        return True

    def thumbnail_path(self, stored_name: str) -> Path | None:
        """A file the lists can show as a small picture: the photo itself when this PC has it,
        else (shared server) a small copy fetched once and kept in the ``.thumbs`` folder."""
        path = self.store / stored_name
        if path.is_file() or not self.shared:
            return path
        thumb = self.store / ".thumbs" / f"{stored_name}.jpg"
        if thumb.is_file():
            return thumb
        with self.db.read_session() as s:
            data = s.scalar(select(AttachmentFile.thumb).join(Attachment, Attachment.sha256 == AttachmentFile.sha256)
                            .where(Attachment.stored_name == stored_name).limit(1))
        if not data:
            return None
        _write_atomic(thumb, data)
        return thumb

    def _upload(self, s: Session, f: StagedFile) -> None:
        """Put the content in the database once (another PC may be adding the same file now)."""
        if not self.shared:
            return
        from sqlalchemy.dialects.postgresql import insert
        thumb = make_thumbnail(f.dest) if f.kind == AttachmentKind.PHOTO.value else None
        s.execute(insert(AttachmentFile).values(sha256=f.sha256, size_bytes=f.size, data=f.dest.read_bytes(),
                                                thumb=thumb, created_at=utcnow())
                  .on_conflict_do_nothing(index_elements=["sha256"]))

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
        self._upload(s, f)
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
            if self.shared:  # the server's copy is what counts; this PC's folder is only a cache
                stored = select(AttachmentFile.sha256)
                return list(s.scalars(select(Attachment).where(Attachment.is_deleted.is_(False),
                                                               Attachment.sha256.not_in(stored))))
            return [a for a in s.scalars(select(Attachment).where(Attachment.is_deleted.is_(False)))
                    if not self.path_of(a).is_file()]


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex[:8]}.part")
    tmp.write_bytes(data)
    tmp.replace(path)
