"""Warehouse locations and bins."""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from wms.core.enums import EntityType, LocationKind
from wms.core.exceptions import DuplicateError, NotFoundError, ValidationError
from wms.core.permissions import P
from wms.db.models import (
    Bin,
    DeliveryNoteLine,
    GoodsIssueLine,
    Location,
    Product,
    ProductionLine,
    ProductionOrder,
    ReturnLine,
    StockAdjustment,
    StockCountLine,
    StockLevel,
    StockMovement,
    StockTransfer,
)
from wms.services import audit, lifecycle
from wms.services.base import BaseService, clean_code, clean_str, normalise_code, prefix_pattern
from wms.services.lifecycle import RemovalPlan

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
                        (Location.id == location_id) | Location.path.like(prefix_pattern(loc.path), escape="\\")
                    )
                else:
                    q = q.where(Bin.location_id == location_id)
            return list(s.scalars(q))

    def find_bin(self, scan: str | None, include_inactive: bool = False) -> Bin | None:
        """The bin a scanned label or typed code refers to: its barcode, its full code
        ("MAIN / A / R03 / S02 / B04", any case) or its short code ("B04") when only one bin has it.
        Used by every screen and by barcode scanners alike."""
        text = (scan or "").strip()
        if not text:
            return None
        with self.db.read_session() as s:
            q = select(Bin)
            if not include_inactive:
                q = q.where(Bin.is_active.is_(True))
            for cond in (Bin.barcode == text, func.upper(Bin.full_code) == text.upper(),
                         func.upper(Bin.code) == text.upper()):
                found = list(s.scalars(q.where(cond).limit(2)))
                if len(found) == 1:
                    return found[0]
                if found:
                    return None  # the short code is ambiguous: scan the label or pick from the list
            return None

    def bin_labels(self, bin_ids: list[int], path, layout: str = "A4_SHEET"):
        """Write printable labels (QR + Code 128 + bin code) for these bins to a PDF; returns the path."""
        from wms.exporters.labels import BinLabel, bin_labels_pdf
        if not bin_ids:
            raise ValidationError("Choose at least one bin.")
        with self.db.read_session() as s:
            bins = sorted((b for b in (s.get(Bin, i) for i in bin_ids) if b is not None), key=lambda b: b.full_code)
            labels = [BinLabel(b.code, b.location.path, b.scan_code) for b in bins]
        return bin_labels_pdf(labels, path, layout)

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
        code = clean_code(data.get("code"), "code", "Code", 30)
        if "/" in code:
            raise ValidationError("Code cannot contain '/'.", "code")
        name = clean_str(data.get("name"), "name", max_len=120) or code
        parent_id = data.get("parent_id")
        with self.db.session() as s:
            parent = s.get(Location, parent_id) if parent_id else None
            if parent is not None and not parent.is_active:
                raise ValidationError(f"{parent.path} is archived - reactivate it first.")
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
                    self._archive_children(s, loc)
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
            select(Bin).join(Location).where((Location.id == loc.id) | Location.path.like(prefix_pattern(loc.path), escape="\\"))
        )
        for b in bins:
            if self.bin_quantity_total(s, b.id):
                raise ValidationError(f"Cannot deactivate: bin {b.full_code} still holds stock.")

    def _archive_children(self, s: Session, loc: Location) -> None:
        """Archiving a location archives everything under it, so its bins can no longer be chosen
        for Goods In or transfers. History still shows them (bins are never deleted)."""
        bins = list(s.scalars(select(Bin).join(Location, Location.id == Bin.location_id).where(
            Bin.is_active.is_(True),
            (Location.id == loc.id) | Location.path.like(prefix_pattern(loc.path), escape="\\"))))
        subs = list(s.scalars(select(Location).where(Location.is_active.is_(True),
                                                     Location.path.like(prefix_pattern(loc.path), escape="\\"))))
        for b in bins:
            b.is_active = False
        for sub in subs:
            sub.is_active = False
        if bins or subs:
            audit.record(s, self.ctx, "LOCATION_ARCHIVED", "LOCATION", loc.id, loc.path,
                         f"{len(subs)} sub-location(s) and {len(bins)} bin(s) archived with it.",
                         new={"bins": [b.full_code for b in bins], "locations": [x.path for x in subs]})

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
        code = clean_code(data.get("code"), "code", "Bin code", 30)
        if "/" in code:
            raise ValidationError("Code cannot contain '/'.", "code")
        with self.db.session() as s:
            loc = s.get(Location, data.get("location_id"))
            if loc is None:
                raise ValidationError("Select the location this bin belongs to.", "location_id")
            if not loc.is_active and data.get("is_active", True):
                raise ValidationError(f"{loc.path} is archived - reactivate the location first.", "location_id")
            dup = s.scalar(select(Bin).where(Bin.location_id == loc.id, Bin.code == code))
            if dup is not None and dup.id != bin_id:
                if not dup.is_active:
                    raise DuplicateError(f"Bin '{code}' already exists in {loc.path} but has been removed from "
                                         f"active use. Restore it instead of creating it again - its history "
                                         f"stays with it.")
                raise DuplicateError(f"Bin '{code}' already exists in {loc.path}.")
            barcode = clean_str(data.get("barcode"), "barcode", max_len=80)
            if barcode:
                clash = s.scalar(select(Bin).where(or_(Bin.barcode == barcode, func.upper(Bin.full_code) ==
                                                       barcode.upper()), Bin.id != (bin_id or 0)))
                if clash is not None:
                    raise DuplicateError(f"Barcode '{barcode}' is already used by bin {clash.full_code}.")
            archiving = restoring = False
            plan = None
            if bin_id:
                b = s.get(Bin, bin_id)
                if b is None:
                    raise NotFoundError("Bin not found.")
                archiving = b.is_active and not data.get("is_active", True)
                restoring = not b.is_active and data.get("is_active", True)
                if archiving:
                    if self.bin_quantity_total(s, b.id):
                        raise ValidationError("Cannot deactivate a bin that still holds stock. Transfer it first.")
                    plan = self._bin_plan(s, b)
                    plan.archive_only = True
                    plan.check()
                    self._clear_default_bin(s, b)
            else:
                b = Bin()
                s.add(b)
            old = {"full_code": b.full_code, "is_available": b.is_available, "is_active": b.is_active,
                   "barcode": b.barcode}
            b.location = loc
            b.code = code
            b.full_code = f"{loc.path} / {code}"
            b.description = clean_str(data.get("description"), max_len=200)
            b.is_available = bool(data.get("is_available", True))
            b.is_active = bool(data.get("is_active", True))
            b.notes = clean_str(data.get("notes"))
            if "barcode" in data:  # screens that do not show the barcode leave it as it is
                b.barcode = barcode
            s.flush()
            o, n = audit.diff(old, {"full_code": b.full_code, "is_available": b.is_available, "is_active": b.is_active,
                                    "barcode": b.barcode})
            if n:
                audit.record(s, self.ctx, "BIN_SAVED", "BIN", b.id, b.full_code, old=o, new=n)
            if archiving:
                lifecycle.record_removal(s, self.ctx, plan, "BIN", None, b.full_code)
            elif restoring:
                lifecycle.record_restore(s, self.ctx, EntityType.BIN.value, b.id, b.full_code, "BIN", None)
            saved_id = b.id
        return self.reload(Bin, saved_id)

    # -- removing bins from use / restoring them ----------------------------------
    def bin_history_count(self, s: Session, bin_id: int) -> int:
        return lifecycle.count(s, StockMovement.id,
                               or_(StockMovement.from_bin_id == bin_id, StockMovement.to_bin_id == bin_id))

    def _bin_plan(self, s: Session, b: Bin) -> RemovalPlan:
        plan = RemovalPlan("Bin", EntityType.BIN.value, b.id, b.code, already_archived=not b.is_active)
        plan.history = self.bin_history_count(s, b.id)
        holding = self.bin_quantity_total(s, b.id)
        if holding:
            plan.blockers.append(f"It still holds stock ({holding} product{'' if holding == 1 else 's'}). "
                                 f"Transfer, issue or adjust it to zero first.")
        blocker = lifecycle.stocktake_blocker(lifecycle.unfinished_counts(s, StockCountLine.bin_id == b.id))
        if blocker:
            plan.blockers.append(blocker)
        plan.references = sum(lifecycle.count(s, col, col == b.id) for col in (
            StockLevel.bin_id, DeliveryNoteLine.bin_id, GoodsIssueLine.bin_id, ReturnLine.bin_id,
            ProductionLine.bin_id, ProductionOrder.output_bin_id, StockAdjustment.bin_id,
            StockTransfer.from_bin_id, StockTransfer.to_bin_id, StockCountLine.bin_id))
        defaults = lifecycle.count(s, Product.id, Product.default_bin_id == b.id)
        if defaults:
            plan.notes.append(f"{defaults} product{' uses' if defaults == 1 else 's use'} it as the default bin - "
                              f"that setting will be cleared.")
        return plan

    def _clear_default_bin(self, s: Session, b: Bin) -> None:
        codes = list(s.scalars(select(Product.internal_code).where(Product.default_bin_id == b.id)))
        if codes:
            s.execute(update(Product).where(Product.default_bin_id == b.id).values(default_bin_id=None)
                      .execution_options(synchronize_session=False))
            audit.record(s, self.ctx, "DEFAULT_BIN_CLEARED", "BIN", b.id, b.full_code,
                         f"Default bin cleared on {len(codes)} product(s) because the bin was removed.",
                         new={"products": codes})

    def bin_removal_plan(self, bin_id: int) -> RemovalPlan:
        """What 'Delete bin' would do, for the confirmation message."""
        with self.db.read_session() as s:
            b = s.get(Bin, bin_id)
            if b is None:
                raise NotFoundError("Bin not found.")
            return self._bin_plan(s, b)

    def remove_bin(self, bin_id: int, reason: str | None = None) -> RemovalPlan:
        """Delete a never-used bin permanently, or take a used one out of active use (history kept)."""
        self.require(P.LOCATIONS_EDIT)
        reason = lifecycle.reason_text(reason)
        with self.db.session() as s:
            b = s.get(Bin, bin_id)
            if b is None:
                raise NotFoundError("Bin not found.")
            plan = self._bin_plan(s, b)
            plan.check()
            self._clear_default_bin(s, b)
            lifecycle.record_removal(s, self.ctx, plan, "BIN", reason, b.full_code)
            if plan.permanent:
                s.delete(b)
            else:
                b.is_active = False
        return plan

    def restore_bin(self, bin_id: int, reason: str | None = None) -> Bin:
        """Put an archived bin back into use. Its old transactions are untouched."""
        self.require(P.LOCATIONS_EDIT)
        reason = lifecycle.reason_text(reason)
        with self.db.session() as s:
            b = s.get(Bin, bin_id)
            if b is None:
                raise NotFoundError("Bin not found.")
            if b.is_active:
                raise ValidationError(f"Bin {b.code} is already in active use.")
            if not b.location.is_active:
                raise ValidationError(f"{b.location.path} has been archived - restore the location first, "
                                      f"then the bin.")
            clash = s.scalar(select(Bin).where(
                Bin.id != b.id, Bin.is_active.is_(True),
                or_(func.upper(Bin.full_code) == b.full_code.upper(),
                    (Bin.location_id == b.location_id) & (func.upper(Bin.code) == b.code.upper()))))
            if clash is not None:
                raise DuplicateError(f"Bin {clash.full_code} is already in active use with the same code. "
                                     f"Restoring {b.code} would make the two impossible to tell apart.")
            b.is_active = True
            s.flush()
            lifecycle.record_restore(s, self.ctx, EntityType.BIN.value, b.id, b.full_code, "BIN", reason)
            saved_id = b.id
        return self.reload(Bin, saved_id)

    def bulk_create_bins(self, location_id: int, prefix: str, start: int, end: int, width: int = 2) -> int:
        """Create e.g. B01..B20 in one go. Existing codes are skipped."""
        self.require(P.LOCATIONS_EDIT)
        if end < start or end - start > 500:
            raise ValidationError("Choose a range of up to 500 bins.")
        prefix = normalise_code(prefix)
        if "/" in prefix or len(prefix) + max(width, len(str(end))) > 30:
            raise ValidationError("Bin prefix is too long or contains '/'.")
        created = 0
        with self.db.session() as s:
            loc = s.get(Location, location_id)
            if loc is None:
                raise NotFoundError("Location not found.")
            if not loc.is_active:
                raise ValidationError(f"{loc.path} is archived - reactivate it first.")
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
