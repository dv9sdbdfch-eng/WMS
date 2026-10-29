"""Stock reserved for projects.

Rules
-----
* A reservation sets stock aside for a project. It is not a stock movement: On Hand does not
  change, Available (usable stock - reserved) goes down.
* Nobody can reserve more than is available (no over-reservation).
* Goods Out to the same project delivers the reservation: each Goods Out line points at the
  reservation it fulfils, and "still reserved" is worked out from those posted lines. Reversing
  the Goods Out puts the quantity back on reserve. When everything has been issued the
  reservation is FULFILLED; RELEASED frees what is left (by hand, or when the project closes).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session

from wms.core.enums import OPEN_SITE_STATUSES, DocumentStatus, EntityType, ReservationStatus
from wms.core.exceptions import NotFoundError, ValidationError
from wms.core.numbers import ZERO, fmt_qty, to_qty, utcnow
from wms.core.permissions import P
from wms.db.models import Bin, GoodsIssue, GoodsIssueLine, Product, Site, StockLevel, StockReservation
from wms.services import audit
from wms.services.base import BaseService, check_client_token, clean_str, like_pattern
from wms.services.numbering import next_number


@dataclass
class ReservationView:
    reservation: StockReservation
    issued: Decimal

    @property
    def remaining(self) -> Decimal:
        """Still set aside (0 once fulfilled or released)."""
        if self.reservation.status != ReservationStatus.ACTIVE.value:
            return ZERO
        return max(self.reservation.quantity - self.issued, ZERO)


# --- shared helpers (also used by Goods Out, products and the integrity check) ----------------

def issued_by_reservation(s: Session, reservation_ids: list[int] | None = None) -> dict[int, Decimal]:
    """Quantity issued against each reservation, from posted Goods Out lines only."""
    q = (select(GoodsIssueLine.reservation_id, GoodsIssueLine.quantity)
         .join(GoodsIssue, GoodsIssue.id == GoodsIssueLine.issue_id)
         .where(GoodsIssue.status == DocumentStatus.POSTED.value, GoodsIssueLine.reservation_id.is_not(None)))
    if reservation_ids is not None:
        if not reservation_ids:
            return {}
        q = q.where(GoodsIssueLine.reservation_id.in_(reservation_ids))
    out: dict[int, Decimal] = {}
    for rid, qty in s.execute(q).all():
        out[rid] = out.get(rid, ZERO) + qty
    return out


def active_reservations(s: Session, product_ids: list[int] | None = None,
                        site_id: int | None = None) -> list[StockReservation]:
    q = select(StockReservation).where(StockReservation.status == ReservationStatus.ACTIVE.value)
    if product_ids is not None:
        q = q.where(StockReservation.product_id.in_(product_ids))
    if site_id is not None:
        q = q.where(StockReservation.site_id == site_id)
    return list(s.scalars(q.order_by(StockReservation.created_at, StockReservation.id)))


def reserved_by_product(s: Session, product_ids: list[int] | None = None,
                        exclude_site_id: int | None = None) -> dict[int, Decimal]:
    """Quantity still reserved per product (active reservations, less what has been issued)."""
    active = [r for r in active_reservations(s, product_ids) if r.site_id != exclude_site_id]
    issued = issued_by_reservation(s, [r.id for r in active])
    out: dict[int, Decimal] = {}
    for r in active:
        left = r.quantity - issued.get(r.id, ZERO)
        if left > ZERO:
            out[r.product_id] = out.get(r.product_id, ZERO) + left
    return out


def usable_stock(s: Session, product_ids: list[int]) -> dict[int, Decimal]:
    """On hand in bins stock can be issued from (quarantine excluded), per product."""
    rows = s.execute(
        select(StockLevel.product_id, StockLevel.quantity)
        .join(Bin, Bin.id == StockLevel.bin_id)
        .where(StockLevel.product_id.in_(product_ids), Bin.is_available.is_(True))).all()
    out: dict[int, Decimal] = {}
    for pid, q in rows:
        out[pid] = out.get(pid, ZERO) + q
    return out


def status_from_issues(r: StockReservation, issued: Decimal) -> str:
    if r.status == ReservationStatus.RELEASED.value:
        return r.status
    return ReservationStatus.FULFILLED.value if issued >= r.quantity else ReservationStatus.ACTIVE.value


def refresh_statuses(s: Session, ctx, reservation_ids: list[int], why: str) -> None:
    if not reservation_ids:
        return
    s.flush()
    issued = issued_by_reservation(s, reservation_ids)
    for r in s.scalars(select(StockReservation).where(StockReservation.id.in_(reservation_ids))):
        new = status_from_issues(r, issued.get(r.id, ZERO))
        if new != r.status:
            audit.record(s, ctx, "RESERVATION_STATUS_CHANGED", EntityType.RESERVATION.value, r.id, r.number, why,
                         old={"status": r.status}, new={"status": new})
            r.status = new


class Issuing:
    """Links Goods Out lines to the project's reservations (used inside GoodsOutService.post).

    A line goes to the oldest reservation of that product for the project that can take all of
    it, else to the oldest one with anything left. Material issued beyond the reservation is
    simply unreserved stock.
    """

    def __init__(self, s: Session, site_id: int):
        self.s = s
        self.reservations = active_reservations(s, site_id=site_id)
        issued = issued_by_reservation(s, [r.id for r in self.reservations])
        self.left = {r.id: r.quantity - issued.get(r.id, ZERO) for r in self.reservations}
        self.touched: list[int] = []

    def reservation_for(self, product_id: int, qty: Decimal) -> int | None:
        mine = [r for r in self.reservations if r.product_id == product_id and self.left[r.id] > ZERO]
        r = next((r for r in mine if self.left[r.id] >= qty), mine[0] if mine else None)
        if r is None:
            return None
        self.left[r.id] -= qty
        if r.id not in self.touched:
            self.touched.append(r.id)
        return r.id


def release_for_closed_site(s: Session, ctx, site: Site) -> int:
    """A completed / cancelled / archived project keeps nothing reserved."""
    n = 0
    for r in active_reservations(s, site_id=site.id):
        r.status, r.released_at, r.released_by_id = ReservationStatus.RELEASED.value, utcnow(), ctx.user.id
        r.release_reason = f"Project {site.project_number} closed."
        audit.record(s, ctx, "RESERVATION_RELEASED", EntityType.RESERVATION.value, r.id, r.number, r.release_reason,
                     old={"status": "ACTIVE"}, new={"status": "RELEASED"})
        n += 1
    return n


# --- service -----------------------------------------------------------------------------------

class ReservationService(BaseService):
    def reserve(self, data: dict) -> ReservationView:
        """Set stock aside for a project. Refused if more than the available stock is asked for."""
        self.require(P.RESERVATIONS_EDIT)
        with self.db.session() as s:
            check_client_token(s, StockReservation, data.get("client_token"), "number",
                               EntityType.RESERVATION.value, "reservation")
            product = s.get(Product, data.get("product_id")) if data.get("product_id") else None
            if product is None:
                raise ValidationError("Choose the product to reserve.", "product_id")
            if not product.is_active:
                raise ValidationError(f"{product.internal_code} has been archived.", "product_id")
            site = s.get(Site, data.get("site_id")) if data.get("site_id") else None
            if site is None:
                raise ValidationError("Choose the project to reserve for.", "site_id")
            if site.status not in OPEN_SITE_STATUSES:
                raise ValidationError(f"Project {site.display} is closed - reopen it to reserve stock for it.",
                                      "site_id")
            qty = self._qty(product, data.get("quantity"))
            self._check_available(s, product, qty)
            r = StockReservation(number=next_number(s, "RESERVATION"), product_id=product.id, site_id=site.id,
                                 site_number_snapshot=site.project_number, site_name_snapshot=site.name,
                                 quantity=qty, required_by=data.get("required_by") or None,
                                 notes=clean_str(data.get("notes")), status=ReservationStatus.ACTIVE.value,
                                 created_by_id=self.user.id, client_token=data.get("client_token") or None)
            s.add(r)
            s.flush()
            audit.record(s, self.ctx, "RESERVATION_CREATED", EntityType.RESERVATION.value, r.id, r.number,
                         f"{fmt_qty(qty)} {product.unit.code} {product.internal_code} for {site.display}",
                         new={"product_id": product.id, "site_id": site.id, "quantity": qty,
                              "required_by": r.required_by})
            rid = r.id
        return self.get(rid)

    def update(self, reservation_id: int, quantity, required_by: date | None = None,
               notes: str | None = None) -> ReservationView:
        """Change an active reservation. It cannot go below what has been issued, and an increase
        must be available."""
        self.require(P.RESERVATIONS_EDIT)
        with self.db.session() as s:
            r = self._active(s, reservation_id)
            qty = self._qty(r.product, quantity)
            issued = issued_by_reservation(s, [r.id]).get(r.id, ZERO)
            if qty < issued:
                raise ValidationError(f"{fmt_qty(issued)} has already been issued against {r.number}, so it cannot "
                                      f"be reduced below that.", "quantity")
            if qty > r.quantity:
                self._check_available(s, r.product, qty - r.quantity)
            old = {"quantity": r.quantity, "required_by": r.required_by, "notes": r.notes}
            r.quantity, r.required_by, r.notes = qty, required_by, clean_str(notes)
            o, n = audit.diff(old, {"quantity": r.quantity, "required_by": r.required_by, "notes": r.notes})
            if n:
                audit.record(s, self.ctx, "RESERVATION_CHANGED", EntityType.RESERVATION.value, r.id, r.number,
                             old=o, new=n)
            refresh_statuses(s, self.ctx, [r.id], "Quantity changed.")
        return self.get(reservation_id)

    def release(self, reservation_id: int, reason: str) -> ReservationView:
        """Free what is still reserved (the project no longer needs it)."""
        self.require(P.RESERVATIONS_EDIT)
        reason = clean_str(reason, "reason", required=True)
        with self.db.session() as s:
            r = self._active(s, reservation_id)
            r.status, r.released_at, r.released_by_id, r.release_reason = (
                ReservationStatus.RELEASED.value, utcnow(), self.user.id, reason)
            audit.record(s, self.ctx, "RESERVATION_RELEASED", EntityType.RESERVATION.value, r.id, r.number, reason,
                         old={"status": "ACTIVE"}, new={"status": "RELEASED"})
        return self.get(reservation_id)

    @staticmethod
    def _qty(product: Product, raw) -> Decimal:
        qty = to_qty(raw if raw not in (None, "") else "0", "quantity")
        if qty <= ZERO:
            raise ValidationError("Quantity must be greater than zero.", "quantity")
        if not product.unit.allow_decimal and qty != qty.to_integral_value():
            raise ValidationError(f"{product.unit.code} quantities must be whole numbers.", "quantity")
        return qty

    @staticmethod
    def _check_available(s: Session, product: Product, qty: Decimal) -> None:
        usable = usable_stock(s, [product.id]).get(product.id, ZERO)
        reserved = reserved_by_product(s, [product.id]).get(product.id, ZERO)
        free = usable - reserved
        if qty > free:
            raise ValidationError(
                f"Only {fmt_qty(max(free, ZERO))} {product.unit.code} of {product.internal_code} is available to "
                f"reserve (on hand {fmt_qty(usable)}, already reserved {fmt_qty(reserved)}).", "quantity")

    @staticmethod
    def _active(s: Session, reservation_id: int) -> StockReservation:
        r = s.get(StockReservation, reservation_id)
        if r is None:
            raise NotFoundError("Reservation not found.")
        if r.status != ReservationStatus.ACTIVE.value:
            raise ValidationError(f"Reservation {r.number} is {r.status.lower()} and can no longer be changed.")
        return r

    def get(self, reservation_id: int) -> ReservationView:
        with self.db.read_session() as s:
            r = s.get(StockReservation, reservation_id)
            if r is None:
                raise NotFoundError("Reservation not found.")
            _ = r.product.unit
            return ReservationView(r, issued_by_reservation(s, [r.id]).get(r.id, ZERO))

    def list(self, status: str | None = None, product_id: int | None = None, site_id: int | None = None,
             text: str | None = None, limit: int = 2000) -> list[ReservationView]:
        q = select(StockReservation).order_by(
            case((StockReservation.status == "ACTIVE", 0), else_=1), StockReservation.required_by.is_(None),
            StockReservation.required_by, StockReservation.id.desc()).limit(limit)
        if status:
            q = q.where(StockReservation.status == status)
        if product_id:
            q = q.where(StockReservation.product_id == product_id)
        if site_id:
            q = q.where(StockReservation.site_id == site_id)
        if text and text.strip():
            like = like_pattern(text)
            q = q.where(or_(
                StockReservation.number.ilike(like, escape="\\"), StockReservation.notes.ilike(like, escape="\\"),
                StockReservation.site_number_snapshot.ilike(like, escape="\\"),
                StockReservation.site_name_snapshot.ilike(like, escape="\\"),
                StockReservation.product_id.in_(select(Product.id).where(or_(
                    Product.internal_code.ilike(like, escape="\\"), Product.name.ilike(like, escape="\\")))),
            ))
        with self.db.read_session() as s:
            rows = list(s.scalars(q).unique())
            _ = [r.product.unit for r in rows]
            issued = issued_by_reservation(s, [r.id for r in rows])
            return [ReservationView(r, issued.get(r.id, ZERO)) for r in rows]

    def conflicts(self, site_id: int | None, lines: list[tuple[int, Decimal]]) -> list[str]:
        """Warnings before a Goods Out: lines that would use stock reserved for other projects."""
        if not lines:
            return []
        totals: dict[int, Decimal] = {}
        for pid, qty in lines:
            if pid and qty:
                totals[pid] = totals.get(pid, ZERO) + Decimal(qty)
        if not totals:
            return []
        out = []
        with self.db.read_session() as s:
            ids = list(totals)
            usable = usable_stock(s, ids)
            others = reserved_by_product(s, ids, exclude_site_id=site_id)
            for pid, qty in totals.items():
                held = others.get(pid, ZERO)
                free = usable.get(pid, ZERO) - held
                if held > ZERO and qty > free:
                    p = s.get(Product, pid)
                    sites = sorted({r.site_label for r in active_reservations(s, [pid]) if r.site_id != site_id})
                    out.append(f"{p.internal_code}: issuing {fmt_qty(qty)} uses {fmt_qty(min(qty - free, held))} "
                               f"{p.unit.code} reserved for {', '.join(sites)}.")
        return out

    def summary_by_product(self, product_ids: list[int]) -> dict[int, Decimal]:
        with self.db.read_session() as s:
            return reserved_by_product(s, product_ids)

    def site_count(self, site_id: int) -> int:
        with self.db.read_session() as s:
            return s.scalar(select(func.count(StockReservation.id)).where(
                StockReservation.site_id == site_id, StockReservation.status == "ACTIVE")) or 0
