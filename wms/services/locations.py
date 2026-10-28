"""Warehouse locations and bins."""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from wms.core.enums import LocationKind
from wms.core.exceptions import DuplicateError, NotFoundError, ValidationError
from wms.core.permissions import P
from wms.db.models import Bin, Location, StockLevel
from wms.services import audit
from wms.services.base import BaseService, clean_str, normalise_code

# Allowed parent kind for each kind (None = top level).
PARENT_KIND = {
    LocationKind.WAREHOUSE: None,
    LocationKind.ZONE: {LocationKind.WAREHOUSE},
    LocationKind.RACK: {LocationKind.WAREHOUSE, LocationKind.ZONE},
    LocationKind.SHELF: {LocationKind.RACK},
}


class LocationService(BaseService):
    def tree(self, include_inactive: bool = False) -> list[Location]:
        """All locations, ordered by path (parents before children)."""
        with self.db.read_session() as s:
            q = select(Location).order_by(Location.path)
            if not include_inactive:
                q = q.where(Location.is_active.is_(True))
            return list(s.scalars(q))

    def bins(self, location_id: int | None = None, include_inactive: bool = False,
             recursive: bool = True) -> list[Bin]:
        with self.db.read_session() as s:
            q = select(Bin).order_by(Bin.full_code)
            if not include_inactive:
                q = q.where(Bin.is_active.is_(True))
            if location_id:
                if recursive:
                    loc = s.get(Location, location_id)
                    q = q.join(Location).where(
                        (Location.id == location_id) | Location.path.like(f"{loc.path} / %")
                    )
                else:
                    q = q.where(Bin.location_id == location_id)
            return list(s.scalars(q))

    def bin_contents(self, bin_id: int) -> list[StockLevel]:
        with self.db.read_session() as s:
            return list(
                s.scalars(
                    select(StockLevel).where(StockLevel.bin_id == bin_id, StockLevel.quantity != Decimal("0"))
                )
            )

    def bin_quantity_total(self, s: Session, bin_id: int) -> int:
        return s.scalar(
            select(func.count(StockLevel.id)).where(StockLevel.bin_id == bin_id, StockLevel.quantity != Decimal("0"))
        ) or 0

    # -- locations ----------------------------------------------------------
    def save_location(self, location_id: int | None, data: dict) -> Location:
        self.require(P.LOCATIONS_EDIT)
        kind = LocationKind(data.get("kind") or LocationKind.WAREHOUSE)
        code = normalise_code(data.get("code"))
        if not code:
            raise ValidationError("Code is required.", "code")
        if "/" in code:
            raise ValidationError("Code cannot contain '/'.", "code")
        name = clean_str(data.get("name"), "name", max_len=120) or code
        parent_id = data.get("parent_id")
        with self.db.session() as s:
            parent = s.get(Location, parent_id) if parent_id else None
            allowed = PARENT_KIND[kind]
            if allowed is None and parent is not None:
                raise ValidationError("A warehouse cannot be inside another location.")
            if allowed is not None and (parent is None or LocationKind(parent.kind) not in allowed):
                names = " or ".join(k.label for k in allowed)
                raise ValidationError(f"A {kind.label.lower()} must be inside a {names.lower()}.")
            dup = s.scalar(select(Location.id).where(Location.parent_id.is_(None) if parent is None
                                                      else Location.parent_id == parent.id, Location.code == code))
            if dup and dup != location_id:
                raise DuplicateError(f"Code '{code}' already exists at this level.")
            if location_id:
                loc = s.get(Location, location_id)
                if loc is None:
                    raise NotFoundError("Location not found.")
                if parent is not None and (parent.id == loc.id or parent.path.startswith(loc.path + " / ")):
                    raise ValidationError("A location cannot be moved inside itself.")
                if not data.get("is_active", True) and loc.is_active:
                    self._ensure_empty(s, loc)
            else:
                loc = Location()
                s.add(loc)
            old = {"code": loc.code, "name": loc.name, "parent": loc.path, "is_active": loc.is_active}
            loc.kind, loc.code, loc.name, loc.parent = kind.value, code, name, parent
            loc.notes = clean_str(data.get("notes"))
            loc.is_active = bool(data.get("is_active", True))
            s.flush()
            self._refresh_paths(s, loc)
            new = {"code": loc.code, "name": loc.name, "parent": loc.path, "is_active": loc.is_active}
            o, n = audit.diff(old, new)
            if n:
                audit.record(s, self.ctx, "LOCATION_SAVED", "LOCATION", loc.id, loc.path, old=o, new=n)
            saved_id = loc.id
        return self.reload(Location, saved_id)

    def _ensure_empty(self, s: Session, loc: Location) -> None:
        bins = s.scalars(
            select(Bin).join(Location).where((Location.id == loc.id) | Location.path.like(f"{loc.path} / %"))
        )
        for b in bins:
            if self.bin_quantity_total(s, b.id):
                raise ValidationError(f"Cannot deactivate: bin {b.full_code} still holds stock.")

    def _refresh_paths(self, s: Session, loc: Location) -> None:
        loc.path = f"{loc.parent.path} / {loc.code}" if loc.parent else loc.code
        for b in loc.bins:
            b.full_code = f"{loc.path} / {b.code}"
        for child in loc.children:
            self._refresh_paths(s, child)
        s.flush()

    # -- bins ---------------------------------------------------------------
    def save_bin(self, bin_id: int | None, data: dict) -> Bin:
        self.require(P.LOCATIONS_EDIT)
        code = normalise_code(data.get("code"))
        if not code:
            raise ValidationError("Bin code is required.", "code")
        if "/" in code:
            raise ValidationError("Code cannot contain '/'.", "code")
        with self.db.session() as s:
            loc = s.get(Location, data.get("location_id"))
            if loc is None:
                raise ValidationError("Select the location this bin belongs to.", "location_id")
            dup = s.scalar(select(Bin.id).where(Bin.location_id == loc.id, Bin.code == code))
            if dup and dup != bin_id:
                raise DuplicateError(f"Bin '{code}' already exists in {loc.path}.")
            if bin_id:
                b = s.get(Bin, bin_id)
                if b is None:
                    raise NotFoundError("Bin not found.")
                if not data.get("is_active", True) and b.is_active and self.bin_quantity_total(s, b.id):
                    raise ValidationError("Cannot deactivate a bin that still holds stock. Transfer it first.")
            else:
                b = Bin()
                s.add(b)
            old = {"full_code": b.full_code, "is_available": b.is_available, "is_active": b.is_active}
            b.location = loc
            b.code = code
            b.full_code = f"{loc.path} / {code}"
            b.description = clean_str(data.get("description"), max_len=200)
            b.is_available = bool(data.get("is_available", True))
            b.is_active = bool(data.get("is_active", True))
            b.notes = clean_str(data.get("notes"))
            s.flush()
            o, n = audit.diff(old, {"full_code": b.full_code, "is_available": b.is_available, "is_active": b.is_active})
            if n:
                audit.record(s, self.ctx, "BIN_SAVED", "BIN", b.id, b.full_code, old=o, new=n)
            saved_id = b.id
        return self.reload(Bin, saved_id)

    def bulk_create_bins(self, location_id: int, prefix: str, start: int, end: int, width: int = 2) -> int:
        """Create e.g. B01..B20 in one go. Existing codes are skipped."""
        self.require(P.LOCATIONS_EDIT)
        if end < start or end - start > 500:
            raise ValidationError("Choose a range of up to 500 bins.")
        prefix = normalise_code(prefix)
        created = 0
        with self.db.session() as s:
            loc = s.get(Location, location_id)
            if loc is None:
                raise NotFoundError("Location not found.")
            existing = set(s.scalars(select(Bin.code).where(Bin.location_id == loc.id)))
            for i in range(start, end + 1):
                code = f"{prefix}{i:0{width}d}"
                if code in existing:
                    continue
                s.add(Bin(location=loc, code=code, full_code=f"{loc.path} / {code}"))
                created += 1
            s.flush()
            audit.record(s, self.ctx, "BINS_CREATED", "LOCATION", loc.id, loc.path,
                         f"{created} bins created ({prefix}{start:0{width}d}-{prefix}{end:0{width}d}).")
        return created
