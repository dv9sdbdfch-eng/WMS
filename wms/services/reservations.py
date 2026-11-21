"""Project stock.

Rules (v1.35, Ihor 21.11.2026: "no reservations - only stock split by project, and issue")
-----------------------------------------------------------------------------------------
* A project's stock is what was delivered for it (Goods In with the project). Each delivery makes
  one row per material in ``stock_reservations`` (the table keeps its old name; hand reservations
  made before v1.35 stay as project stock too). It is not a stock movement: On Hand does not
  change; General stock (usable stock - all project stock) goes down.
* Goods Out and production for the project use its own stock first: each line points at the row
  it uses, and what is left is worked out from those posted lines (so a reversal puts it back).
  When everything has been used the row is FULFILLED.
* Other projects' stock cannot be issued; a Store Manager can allow it with a reason, and the stock
  moves off the other project with a PSR record. Return to main (PSR) gives stock back to the
  warehouse; closing a project does that for whatever it still holds.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import case, func, or_, select, union_all
from sqlalchemy.orm import Session

from wms.core.enums import OPEN_SITE_STATUSES, DocumentStatus, EntityType, ReservationStatus
from wms.core.exceptions import NotFoundError, ValidationError
from wms.core.numbers import ZERO, fmt_qty, to_qty, utcnow
from wms.core.permissions import P
from wms.db.models import (
    Bin,
    GoodsIssue,
    GoodsIssueLine,
    Product,
    ProductionLine,
    ProductionOrder,
    ProjectStockRelease,
    Site,
    StockLevel,
    StockReservation,
)
from wms.services import audit
from wms.services.base import BaseService, clean_str, like_pattern
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

def _used_lines():
    """(reservation_id, quantity) of posted lines that use project stock: Goods Out to the
    project and (v1.35) production for the project."""
    gi = (select(GoodsIssueLine.reservation_id.label("rid"), GoodsIssueLine.quantity.label("qty"))
          .join(GoodsIssue, GoodsIssue.id == GoodsIssueLine.issue_id)
          .where(GoodsIssue.status == DocumentStatus.POSTED.value, GoodsIssueLine.reservation_id.is_not(None)))
    pr = (select(ProductionLine.reservation_id.label("rid"), ProductionLine.quantity.label("qty"))
          .join(ProductionOrder, ProductionOrder.id == ProductionLine.order_id)
          .where(ProductionOrder.status == DocumentStatus.POSTED.value, ProductionLine.reservation_id.is_not(None)))
    return union_all(gi, pr).subquery()


def used_query():
    """(rid, q): quantity used from each project stock row, as SQL (grouped subquery)."""
    u = _used_lines()
    return select(u.c.rid.label("rid"), func.sum(u.c.qty).label("q")).group_by(u.c.rid).subquery()


def issued_by_reservation(s: Session, reservation_ids: list[int] | None = None) -> dict[int, Decimal]:
    """Quantity used from each project stock row: posted Goods Out and production lines."""
    u = _used_lines()
    q = select(u.c.rid, func.sum(u.c.qty)).group_by(u.c.rid)
    if reservation_ids is not None:
        if not reservation_ids:
            return {}
        q = q.where(u.c.rid.in_(reservation_ids))
    return {rid: Decimal(qty) for rid, qty in s.execute(q).all()}


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


def reserved_query():
    """(product_id, reserved) for every product with stock still reserved - the same figure as
    reserved_by_product, as SQL, so search can filter and sort on Available without loading rows."""
    issued = used_query()
    left = StockReservation.quantity - func.coalesce(issued.c.q, 0)
    return (select(StockReservation.product_id.label("product_id"),
                   func.sum(case((left > 0, left), else_=0)).label("reserved"))
            .outerjoin(issued, issued.c.rid == StockReservation.id)
            .where(StockReservation.status == ReservationStatus.ACTIVE.value)
            .group_by(StockReservation.product_id))


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
    closed: dict[int, Site] = {}
    for r in s.scalars(select(StockReservation).where(StockReservation.id.in_(reservation_ids))):
        new = status_from_issues(r, issued.get(r.id, ZERO))
        if new != r.status:
            audit.record(s, ctx, "RESERVATION_STATUS_CHANGED", EntityType.RESERVATION.value, r.id, r.number, why,
                         old={"status": r.status}, new={"status": new})
            r.status = new
        if r.status == ReservationStatus.ACTIVE.value and r.site.status not in OPEN_SITE_STATUSES:
            closed[r.site_id] = r.site
    # v1.35: a reversal after the project was closed must not leave stock on a closed project -
    # it goes straight back to main stock, recorded (PSR) in the project's history
    for site in closed.values():
        s.flush()
        release_for_closed_site(s, ctx, site, f"{why} after the project was closed")


class Issuing:
    """Links Goods Out lines to the project's reservations (used inside GoodsOutService.post).

    A line goes to the oldest reservation of that product for the project that can take all of
    it; otherwise (v1.26) it is shared over the project's reservations, oldest first, so all the
    project's stock is used before unreserved stock. Material issued beyond that is simply
    unreserved stock.
    """

    def __init__(self, s: Session, site_id: int):
        self.s = s
        self.reservations = active_reservations(s, site_id=site_id)
        issued = issued_by_reservation(s, [r.id for r in self.reservations])
        self.left = {r.id: r.quantity - issued.get(r.id, ZERO) for r in self.reservations}
        self.touched: list[int] = []

    def split(self, product_id: int, qty: Decimal) -> list[tuple[int | None, Decimal]]:
        """v1.26: share ``qty`` over the project's reservations of the product so that everything
        set aside for the project (delivered for it or reserved) is used before unreserved stock:
        [(reservation id or None, quantity)]. A quantity one reservation can take stays in one part."""
        mine = [r for r in self.reservations if r.product_id == product_id and self.left[r.id] > ZERO]
        fit = next((r for r in mine if self.left[r.id] >= qty), None)
        chosen = [(fit, qty)] if fit else []
        rest = ZERO if fit else qty
        if not fit:
            for r in mine:
                if rest <= ZERO:
                    break
                take = min(rest, self.left[r.id])
                chosen.append((r, take))
                rest -= take
        parts: list[tuple[int | None, Decimal]] = []
        for r, take in chosen:
            self.left[r.id] -= take
            if r.id not in self.touched:
                self.touched.append(r.id)
            parts.append((r.id, take))
        if rest > ZERO:
            parts.append((None, rest))
        return parts


def release_for_closed_site(s: Session, ctx, site: Site, reason: str | None = None) -> str | None:
    """A completed / cancelled / archived project keeps no stock. v1.35 (Ihor 21.11.2026): what it
    still holds goes back to main stock with a Return to main record (PSR) and the reason, so the
    leftover is documented in the project's history. Returns the PSR number, or None if nothing was
    left."""
    why = clean_str(reason) or f"Project {site.project_number} closed"
    held = project_stock(s, site.id)
    number = next_number(s, "PROJECT_RELEASE") if held else None
    for pid, line in sorted(held.items()):
        give_back(s, ctx, site, pid, line.total, number, f"Project closed: {why}",
                  f"Returned to main stock ({number}): project closed - {why}", "back to main stock (project closed)")
    # rows with nothing left (all issued) are simply closed
    for r in active_reservations(s, site_id=site.id):
        r.status, r.released_at, r.released_by_id = ReservationStatus.RELEASED.value, utcnow(), ctx.user.id
        r.release_reason = f"Project {site.project_number} closed."
        audit.record(s, ctx, "RESERVATION_RELEASED", EntityType.RESERVATION.value, r.id, r.number, r.release_reason,
                     old={"status": "ACTIVE"}, new={"status": "RELEASED"})
    return number


def reserve_delivery(s: Session, ctx, dn, site: Site, totals: dict[int, Decimal]) -> list[int]:
    """v1.26 Goods In for a project: everything the delivery brought in is set aside for the
    project (one reservation per product, linked to the delivery). No availability check - the
    stock has just arrived for this project. Returns the reservation ids."""
    ids = []
    for pid, qty in totals.items():
        if qty <= ZERO:
            continue
        product = s.get(Product, pid)
        r = StockReservation(number=next_number(s, "RESERVATION"), product_id=pid, site_id=site.id,
                             site_number_snapshot=site.project_number, site_name_snapshot=site.name,
                             quantity=qty, notes=f"Delivered for the project: delivery note {dn.dn_number}",
                             status=ReservationStatus.ACTIVE.value, created_by_id=ctx.user.id,
                             delivery_note_id=dn.id)
        s.add(r)
        s.flush()
        audit.record(s, ctx, "RESERVATION_CREATED", EntityType.RESERVATION.value, r.id, r.number,
                     f"{fmt_qty(qty)} {product.unit.code} {product.internal_code} for {site.display} "
                     f"(delivery note {dn.dn_number})",
                     new={"product_id": pid, "site_id": site.id, "quantity": qty, "delivery_note_id": dn.id})
        ids.append(r.id)
    return ids


def unreserve_delivery(s: Session, ctx, dn, why: str) -> None:
    """A reversed delivery no longer belongs to the project: what is still set aside from it is
    freed. Anything already issued to the project stays issued (the reservation is closed there)."""
    rows = list(s.scalars(select(StockReservation).where(StockReservation.delivery_note_id == dn.id,
                                                         StockReservation.status == ReservationStatus.ACTIVE.value)))
    issued = issued_by_reservation(s, [r.id for r in rows])
    for r in rows:
        done = issued.get(r.id, ZERO)
        old = {"status": r.status, "quantity": r.quantity}
        if done > ZERO:
            r.quantity, r.status = done, ReservationStatus.FULFILLED.value
        else:
            r.status, r.released_at, r.released_by_id, r.release_reason = (
                ReservationStatus.RELEASED.value, utcnow(), ctx.user.id, why)
        audit.record(s, ctx, "RESERVATION_RELEASED", EntityType.RESERVATION.value, r.id, r.number, why,
                     old=old, new={"status": r.status, "quantity": r.quantity})


@dataclass
class ProjectStockLine:
    """What a project holds of one material: set aside for it and not yet issued."""

    product_id: int
    code: str
    name: str
    unit: str
    from_deliveries: Decimal   # delivered for the project (Goods In with the project)
    reserved: Decimal          # set aside by hand from warehouse stock

    @property
    def total(self) -> Decimal:
        return self.from_deliveries + self.reserved


def project_stock(s: Session, site_id: int, product_ids: list[int] | None = None) -> dict[int, ProjectStockLine]:
    """{product_id: ProjectStockLine} - the project's stock (active reservations less what was issued)."""
    rows = [r for r in active_reservations(s, product_ids, site_id=site_id)]
    issued = issued_by_reservation(s, [r.id for r in rows])
    out: dict[int, ProjectStockLine] = {}
    for r in rows:
        left = max(r.quantity - issued.get(r.id, ZERO), ZERO)
        if left <= ZERO:
            continue
        line = out.get(r.product_id)
        if line is None:
            p = r.product
            line = out[r.product_id] = ProjectStockLine(p.id, p.internal_code, p.name, p.unit.code, ZERO, ZERO)
        if r.delivery_note_id:
            line.from_deliveries += left
        else:
            line.reserved += left
    return out


def give_back(s: Session, ctx, site: Site, product_id: int, qty: Decimal, number: str, reason: str,
              why: str, where: str) -> None:
    """Take ``qty`` of a material off a project's stock (newest rows first) and record it as a
    ProjectStockRelease ``number``: Return to main, a project closed with stock left (v1.35), or
    stock moved to another project by a Goods Out a manager allowed (v1.35). Nothing moves on the
    shelves; the ledger is not touched."""
    rows = list(s.scalars(select(StockReservation).where(
        StockReservation.product_id == product_id, StockReservation.site_id == site.id,
        StockReservation.status == ReservationStatus.ACTIVE.value).order_by(StockReservation.id.desc())
        .with_for_update(of=StockReservation).execution_options(populate_existing=True)))
    issued = issued_by_reservation(s, [r.id for r in rows])
    rest = qty
    for r in rows:
        if rest <= ZERO:
            break
        done = issued.get(r.id, ZERO)
        left = r.quantity - done
        if left <= ZERO:
            continue
        take = min(rest, left)
        rest -= take
        old = {"quantity": r.quantity, "status": r.status}
        r.quantity -= take
        if r.quantity <= done:
            if done > ZERO:
                r.quantity, r.status = done, ReservationStatus.FULFILLED.value
            else:
                r.quantity = old["quantity"]  # quantity must stay > 0: keep it, mark released
                r.status, r.released_at, r.released_by_id, r.release_reason = (
                    ReservationStatus.RELEASED.value, utcnow(), ctx.user.id, why)
        audit.record(s, ctx, "RESERVATION_CHANGED", EntityType.RESERVATION.value, r.id, r.number,
                     why, old=old, new={"quantity": r.quantity, "status": r.status})
    p = s.get(Product, product_id)
    s.add(ProjectStockRelease(number=number, site_id=site.id, site_number_snapshot=site.project_number,
                              site_name_snapshot=site.name, product_id=product_id, quantity=qty, reason=reason,
                              user_id=ctx.user.id, user_name_snapshot=ctx.user.full_name))
    audit.record(s, ctx, "PROJECT_STOCK_RETURNED", EntityType.SITE.value, site.id, site.display,
                 f"{number}: {fmt_qty(qty)} {p.unit.code} {p.internal_code} {where} - {reason}",
                 new={"number": number, "product_id": product_id, "quantity": qty})


def from_other_projects(s: Session, site_id: int | None, totals: dict[int, Decimal]) -> dict[int, Decimal]:
    """{product_id: quantity} that issuing ``totals`` to ``site_id`` would take out of other projects'
    stock: what is left after the project's own stock and the general stock are used."""
    if not totals:
        return {}
    ids = list(totals)
    usable = usable_stock(s, ids)
    others = reserved_by_product(s, ids, exclude_site_id=site_id)
    out = {}
    for pid, qty in totals.items():
        held = others.get(pid, ZERO)
        free = usable.get(pid, ZERO) - held
        if held > ZERO and qty > free:
            out[pid] = min(qty - max(free, ZERO), held)
    return out


def other_projects_message(s: Session, totals: dict[int, Decimal], from_others: dict[int, Decimal],
                           what: str) -> str:
    """The block message: how much would come from other projects and how much may go without a manager."""
    parts = []
    for pid, q in from_others.items():
        p = s.get(Product, pid)
        ok = totals[pid] - q
        parts.append(f"{p.internal_code}: {fmt_qty(q)} {p.unit.code} from other projects - "
                     f"you can use up to {fmt_qty(ok)} {p.unit.code} without a manager")
    return (f"{what} would use stock that belongs to other projects.\n" + "\n".join(parts) +
            "\nOnly a Store Manager can allow that, with a reason.")


def take_from_other_projects(s: Session, ctx, site: Site | None, needed: dict[int, Decimal], doc_number: str,
                             reason: str) -> list[dict]:
    """v1.35 (Ihor 21.11.2026): a manager allowed a Goods Out or production to use other projects'
    stock. That stock is moved off those projects (newest stock first) with a PSR record in their
    history. Returns [{"site_id", "product_id", "quantity"}] so a reversal can give it back."""
    number = None
    moved: list[dict] = []
    to = site.display if site else "production without a project"
    for pid, qty in needed.items():
        rest = qty
        per_site: dict[int, Decimal] = {}
        rows = sorted((r for r in active_reservations(s, [pid]) if site is None or r.site_id != site.id),
                      key=lambda r: r.id, reverse=True)
        issued = issued_by_reservation(s, [r.id for r in rows])
        for r in rows:
            if rest <= ZERO:
                break
            take = min(rest, max(r.quantity - issued.get(r.id, ZERO), ZERO))
            if take > ZERO:
                per_site[r.site_id] = per_site.get(r.site_id, ZERO) + take
                rest -= take
        for sid, q in per_site.items():
            number = number or next_number(s, "PROJECT_RELEASE")
            other = s.get(Site, sid)
            give_back(s, ctx, other, pid, q, number, reason,
                      f"Moved to {to} on {doc_number} ({number}): {reason}", f"moved to {to} on {doc_number}")
            moved.append({"site_id": sid, "product_id": pid, "quantity": str(q)})
    return moved


def give_moved_stock_back(s: Session, ctx, moved: list[dict], doc_number: str) -> None:
    """v1.35: the document that took other projects' stock was reversed, so the material is back on
    the shelf: it becomes those projects' stock again (if they are still open), as a new row that
    points at nothing but says why. A closed project's share stays in main stock."""
    for m in moved:
        site = s.get(Site, m["site_id"])
        if site is None or site.status not in OPEN_SITE_STATUSES:
            continue
        product = s.get(Product, m["product_id"])
        qty = to_qty(m["quantity"])
        r = StockReservation(number=next_number(s, "RESERVATION"), product_id=product.id, site_id=site.id,
                             site_number_snapshot=site.project_number, site_name_snapshot=site.name,
                             quantity=qty, notes=f"Given back: {doc_number} was reversed",
                             status=ReservationStatus.ACTIVE.value, created_by_id=ctx.user.id)
        s.add(r)
        s.flush()
        audit.record(s, ctx, "RESERVATION_CREATED", EntityType.RESERVATION.value, r.id, r.number,
                     f"{fmt_qty(qty)} {product.unit.code} {product.internal_code} back to {site.display}: "
                     f"{doc_number} was reversed",
                     new={"product_id": product.id, "site_id": site.id, "quantity": qty})


def moved_by(s: Session, entity_type: str, entity_id: int) -> list[dict]:
    """What a Goods Out / production took from other projects (from its audit record)."""
    import json

    from wms.db.models import AuditLog
    out: list[dict] = []
    for row in s.scalars(select(AuditLog).where(AuditLog.action == "PROJECT_STOCK_TAKEN",
                                                AuditLog.entity_type == entity_type,
                                                AuditLog.entity_id == entity_id)):
        out += json.loads(row.new_values or "{}").get("moved", [])
    return out


# --- service -----------------------------------------------------------------------------------

class ReservationService(BaseService):
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
        """Warnings before a Goods Out: lines that would use other projects' stock."""
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
                    out.append(f"{p.internal_code}: issuing {fmt_qty(qty)} uses {fmt_qty(min(qty - max(free, ZERO), held))} "
                               f"{p.unit.code} of the project stock of {', '.join(sites)}.")
        return out

    def project_stock(self, site_id: int) -> list[ProjectStockLine]:
        """The project's stock per material, sorted by code: delivered for it + reserved for it,
        less what has been issued to it."""
        with self.db.read_session() as s:
            if s.get(Site, site_id) is None:
                raise NotFoundError("Project not found.")
            return sorted(project_stock(s, site_id).values(), key=lambda x: x.code)

    def return_to_main(self, site_id: int, items: list[tuple[int, object]], reason: str) -> str:
        """v1.27: give material back from a project's stock to the main warehouse stock.

        ``items`` = [(product_id, quantity)]. Each quantity can be at most what the project holds of
        that material. What was set aside for the project is reduced by it - the newest reservations
        first - so the material is free for any work again. Nothing moves and the ledger is not
        touched. All lines or none; returns the document number (PSR-000001)."""
        self.require(P.RESERVATIONS_EDIT)
        reason = clean_str(reason, "reason", required=True)
        wanted: dict[int, Decimal] = {}
        for pid, raw in items:
            if raw in (None, "") or not pid:
                continue
            qty = to_qty(raw, "quantity")
            if qty < ZERO:
                raise ValidationError("Quantity cannot be negative.", "quantity")
            if qty > ZERO:
                wanted[pid] = wanted.get(pid, ZERO) + qty
        if not wanted:
            raise ValidationError("Enter the quantity to return for at least one material.")
        with self.db.session() as s:
            site = s.get(Site, site_id)
            if site is None:
                raise NotFoundError("Project not found.")
            held = project_stock(s, site_id, list(wanted))
            for pid, qty in wanted.items():
                have = held[pid].total if pid in held else ZERO
                if qty > have:
                    p = s.get(Product, pid)
                    raise ValidationError(f"{p.internal_code}: the project holds {fmt_qty(have)} {p.unit.code}, "
                                          f"so {fmt_qty(qty)} cannot be returned.", "quantity")
            number = next_number(s, "PROJECT_RELEASE")
            for pid, qty in wanted.items():
                give_back(s, self.ctx, site, pid, qty, number, reason,
                          f"Returned to main stock ({number}): {reason}", "back to main stock")
            return number

    def project_releases(self, site_id: int) -> list[ProjectStockRelease]:
        """Returns to main stock of a project, newest first."""
        with self.db.read_session() as s:
            rows = list(s.scalars(select(ProjectStockRelease).where(ProjectStockRelease.site_id == site_id)
                                  .order_by(ProjectStockRelease.id.desc())))
            _ = [r.product.unit for r in rows]
            return rows

    def summary_by_product(self, product_ids: list[int]) -> dict[int, Decimal]:
        with self.db.read_session() as s:
            return reserved_by_product(s, product_ids)

    def site_count(self, site_id: int) -> int:
        with self.db.read_session() as s:
            return s.scalar(select(func.count(StockReservation.id)).where(
                StockReservation.site_id == site_id, StockReservation.status == "ACTIVE")) or 0
