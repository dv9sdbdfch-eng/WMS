"""The Materials workspace: one query definition for the list, its count, its export and the
Project -> Materials tab.

Everything the person can filter on is a field of ``MaterialFilter``. ``MaterialsService`` turns a
filter into ONE SQL statement (``_query``) and everything else is built from that statement:

* ``count``      - "Showing 42 materials" (the same WHERE clause, counted by the database);
* ``page``       - one screenful of rows for the table (LIMIT / OFFSET; the table asks for the next
                   page when the person scrolls, so there is no "first 500 only" limit);
* ``iter_rows``  - every matching row in the same order, in chunks, for "Export Current View";
* ``rows_for``   - the chosen rows, for "Export Selected".

Because the exporter and the screen read the same statement, the export always contains exactly
what the screen shows (spec section 21) - the filter logic exists only here.

Scaling (spec sections 17, 42): filtering, sorting and counting happen in the database; the
screen only ever holds the rows it has shown. Stock totals (On Hand / Available / Reserved /
Incoming) are joined into the statement only when a stock filter or stock sort needs them; for a
plain page they are fetched for that page's rows only, with one grouped query each - never one
query per row.

Project figures (spec section 6; UI refactor spec 24) are separate things and are never mixed up:

* Required   - the project material list (``project_materials``; Excel / manual entry);
* Reserved   - active reservations for the project still to be issued (stock stays in its bin);
* Issued     - posted Goods Out to the project (reversals netted off);
* Returned   - posted returns from the project (reversals netted off);
* Net Issued - Issued - Returned (what is physically on the project now);
* Remaining  - Required - Net Issued - Reserved, never below 0: what is neither on site nor set
               aside yet, i.e. still to be reserved or issued. Blank when the material is not on
               the project's list (no Required quantity).

None of them changes stock; only Goods In / Out / Transfer / Return / Adjustment / Stocktake do.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field, fields
from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import and_, case, delete, exists, func, or_, select, true, type_coerce, update
from sqlalchemy.orm import Session, selectinload

from wms.core.enums import (
    CLOSED_SITE_STATUSES,
    OPEN_PO_STATUSES,
    AttachmentKind,
    DocumentStatus,
    EntityType,
    ItemType,
    MovementType,
    ReservationStatus,
)
from wms.core.exceptions import NotFoundError, ValidationError
from wms.core.numbers import ZERO, to_qty, utcnow
from wms.core.permissions import P
from wms.db.models import (
    Attachment,
    Bin,
    Category,
    DeliveryNote,
    DeliveryNoteLine,
    GoodsIssue,
    GoodsIssueLine,
    Location,
    Product,
    ProjectMaterial,
    PurchaseOrder,
    PurchaseOrderLine,
    SavedSearch,
    Site,
    StockLevel,
    StockMovement,
    StockReservation,
    Supplier,
)
from wms.db.types import Quantity
from wms.services import audit
from wms.services.base import BaseService, clean_str, like_pattern, prefix_pattern
from wms.services.products import code_key, code_key_sql, key_pattern, search_terms
from wms.services.stock_engine import product_totals_query

PAGE_SIZE = 200  # rows fetched per scroll step - a screen shows ~25, so this is ~8 screens

# --- choices shown in the filter drop-downs (value -> label) ---------------------------------------

STOCK_CHOICES = {
    "IN_STOCK": "In Stock",
    "LOW_STOCK": "Low Stock",
    "OUT_OF_STOCK": "Out of Stock",
    "RESERVED": "Reserved",
    "AVAILABLE": "Available",
    "INCOMING": "Incoming",
}
BIN_STATES = {
    "NONE": "No Bin Assigned",
    "MULTIPLE": "Multiple Bins",
    "ACTIVE": "In Active Bins",
}
STATUS_CHOICES = {"ACTIVE": "Active", "ARCHIVED": "Archived", "ALL": "Active and archived"}
PRESENCE = {"WITH": "Has", "WITHOUT": "Missing"}
PROJECT_STATUS_CHOICES = {"OPEN": "Open projects", "CLOSED": "Completed / closed projects"}
ITEM_TYPES = {"RAW": "Materials and components", "FINISHED": "Finished products"}

#: sort keys the table header understands (column -> key)
SORT_KEYS = ("code", "name", "product_no", "supplier", "category", "brand", "qty", "available", "reserved",
             "incoming", "bin", "status", "received", "issued_date", "updated", "created",
             "required", "proj_reserved", "issued", "returned", "net", "remaining")
STOCK_SORTS = {"qty", "available", "reserved", "incoming", "status"}
PROJECT_SORTS = {"required", "proj_reserved", "issued", "returned", "net", "remaining"}


def remaining_qty(required: Decimal | None, net_issued: Decimal, reserved: Decimal) -> Decimal | None:
    """Remaining = Required - Net Issued - Reserved, never below 0 (see the module docstring)."""
    if required is None:
        return None
    left = required - net_issued - reserved
    return left if left > 0 else ZERO

RECENT_DAYS = 30


@dataclass
class MaterialFilter:
    """Everything that defines the Materials list. Empty / None = "no filter"."""

    text: str = ""
    # the four primary filters
    site_id: int | None = None
    supplier_id: int | None = None
    bin_id: int | None = None
    bin_state: str | None = None  # BIN_STATES key
    stock: str | None = None  # STOCK_CHOICES key
    # More Filters
    category_id: int | None = None
    brand: str | None = None
    warehouse_id: int | None = None  # a location (warehouse / zone / rack / shelf)
    project_status: str | None = None  # PROJECT_STATUS_CHOICES key
    status: str = "ACTIVE"  # STATUS_CHOICES key
    photo: str | None = None  # WITH / WITHOUT
    has_supplier: str | None = None  # WITH / WITHOUT
    item_type: str | None = None  # RAW / FINISHED
    received_from: date | None = None
    received_to: date | None = None
    issued_from: date | None = None
    issued_to: date | None = None
    created_from: date | None = None
    created_to: date | None = None
    updated_from: date | None = None
    updated_to: date | None = None
    # order of the rows
    sort: str = "code"
    descending: bool = False

    DATE_FIELDS = ("received_from", "received_to", "issued_from", "issued_to", "created_from", "created_to",
                   "updated_from", "updated_to")

    # -- (de)serialisation, for saved views ---------------------------------------------------
    def to_dict(self) -> dict:
        out = {}
        for f in fields(self):
            v = getattr(self, f.name)
            if v in (None, "") or (f.name == "status" and v == "ACTIVE") or (f.name == "sort" and v == "code") \
                    or (f.name == "descending" and not v):
                continue
            out[f.name] = v.isoformat() if isinstance(v, date) else v
        return out

    @classmethod
    def from_dict(cls, data: dict | None) -> MaterialFilter:
        known = {f.name for f in fields(cls)}
        f = cls()
        for k, v in (data or {}).items():
            if k not in known or v is None:
                continue  # a view saved by another version: ignore what is not understood
            if k in cls.DATE_FIELDS:
                try:
                    v = date.fromisoformat(v)
                except (TypeError, ValueError):
                    continue
            setattr(f, k, v)
        return f

    def copy(self) -> MaterialFilter:
        return MaterialFilter.from_dict(self.to_dict())

    def validate(self) -> None:
        if self.stock and self.stock not in STOCK_CHOICES:
            raise ValidationError(f"Unknown stock filter {self.stock}.")
        if self.bin_state and self.bin_state not in BIN_STATES:
            raise ValidationError(f"Unknown bin filter {self.bin_state}.")
        if self.status not in STATUS_CHOICES:
            raise ValidationError(f"Unknown status filter {self.status}.")
        if self.sort not in SORT_KEYS:
            self.sort = "code"
        if self.sort in PROJECT_SORTS and not self.site_id:
            self.sort = "code"
        for a, b in (("received_from", "received_to"), ("issued_from", "issued_to"),
                     ("created_from", "created_to"), ("updated_from", "updated_to")):
            lo, hi = getattr(self, a), getattr(self, b)
            if lo and hi and lo > hi:
                raise ValidationError("A 'from' date is after its 'to' date.")

    @property
    def needs_stock(self) -> bool:
        return bool(self.stock) or self.sort in STOCK_SORTS

    def is_default(self) -> bool:
        return not self.to_dict().keys() - {"sort", "descending"}


@dataclass
class BinStock:
    code: str
    full_code: str
    quantity: Decimal
    is_active: bool


@dataclass
class MaterialRow:
    """One line of the Materials table (and of its Excel export)."""

    product: Product
    on_hand: Decimal = ZERO
    usable: Decimal = ZERO
    reserved: Decimal = ZERO
    incoming: Decimal = ZERO
    bins: list[BinStock] = field(default_factory=list)
    photo_id: int | None = None
    photo_name: str | None = None  # stored file name of that photo (thumbnail in the table)
    # project figures - only filled when the list is filtered by a project
    required: Decimal | None = None
    proj_reserved: Decimal = ZERO
    issued: Decimal = ZERO
    returned: Decimal = ZERO
    on_list: bool = False  # on the project's material list (else: only reserved / issued)

    @property
    def id(self) -> int:
        return self.product.id

    @property
    def available(self) -> Decimal:
        return self.usable - self.reserved

    @property
    def net(self) -> Decimal:
        return self.issued - self.returned

    @property
    def remaining(self) -> Decimal | None:
        return remaining_qty(self.required, self.net, self.proj_reserved)

    @property
    def reorder_point(self) -> Decimal | None:
        p = self.product
        level = p.reorder_level if p.reorder_level and p.reorder_level > 0 else None
        minimum = p.min_stock if p.min_stock and p.min_stock > 0 else None
        if level is not None and (minimum is None or level > minimum):
            return level
        return minimum

    @property
    def status_key(self) -> str:
        """ARCHIVED / OUT / LOW / IN - judged on the totals over every bin."""
        if not self.product.is_active:
            return "ARCHIVED"
        if self.on_hand <= 0:
            return "OUT"
        rp = self.reorder_point
        if rp is not None and self.available <= rp:
            return "LOW"
        return "IN"

    @property
    def status_label(self) -> str:
        return {"ARCHIVED": "Archived", "OUT": "Out of Stock", "LOW": "Low Stock", "IN": "In Stock"}[self.status_key]

    @property
    def bin_text(self) -> str:
        if not self.bins:
            b = self.product.default_bin
            return f"({b.code})" if b is not None else ""
        first = self.bins[0].code
        return first if len(self.bins) == 1 else f"{first} +{len(self.bins) - 1}"

    @property
    def bins_detail(self) -> str:
        return ", ".join(f"{b.full_code}: {b.quantity.normalize():f}" for b in self.bins)


@dataclass
class ProjectSummary:
    site: Site
    materials: int
    required: Decimal
    reserved: Decimal
    issued: Decimal
    returned: Decimal
    remaining: Decimal = ZERO  # sum of each material's Remaining (not the totals' difference)

    @property
    def net(self) -> Decimal:
        return self.issued - self.returned


@dataclass
class SavedView:
    id: int | None  # None = built in
    name: str
    filter: MaterialFilter


BUILTIN_VIEWS: tuple[tuple[str, dict], ...] = (
    ("All Materials", {}),
    ("Low Stock", {"stock": "LOW_STOCK"}),
    ("No Bin", {"bin_state": "NONE"}),
    ("Missing Photos", {"photo": "WITHOUT"}),
    ("Recently Received", {"received_from": "__recent__", "sort": "received", "descending": True}),
    ("Finished Products", {"item_type": "FINISHED"}),
)

VIEW_PREFIX = '{"materials":'  # saved_searches rows that belong to this screen, not the Search page


# --- SQL building blocks -------------------------------------------------------------------------

def _q(expr):
    return type_coerce(expr, Quantity())


def _start(d: date) -> datetime:
    return datetime.combine(d, datetime.min.time()).astimezone()


def _end(d: date) -> datetime:
    return datetime.combine(d + timedelta(days=1), datetime.min.time()).astimezone()


def material_text_filter(text: str):
    """The search box (spec section 3): every word must be found - case-insensitive, anywhere
    inside - in the code, product number, barcode, name, description, brand or supplier name.
    Codes also match typed with other spacing / punctuation ('h 3311' finds H3311)."""
    terms = search_terms(text)
    if not terms:
        return true()

    def term(t: str):
        like = like_pattern(t)
        return or_(Product.internal_code.ilike(like, escape="\\"),
                   Product.supplier_code.ilike(like, escape="\\"),
                   Product.barcode.ilike(like, escape="\\"),
                   Product.name.ilike(like, escape="\\"),
                   Product.description.ilike(like, escape="\\"),
                   Product.brand.ilike(like, escape="\\"),
                   Product.supplier_id.in_(select(Supplier.id).where(Supplier.name.ilike(like, escape="\\"))))

    words = and_(*(term(t) for t in terms))
    key = code_key(text)
    if len(key) < 2 or key == text.strip().upper():
        return words  # nothing to normalise: the plain match already covers it
    pat = key_pattern(key)
    return or_(words, code_key_sql(Product.internal_code).like(pat, escape="\\"),
               code_key_sql(Product.supplier_code).like(pat, escape="\\"),
               code_key_sql(Product.barcode).like(pat, escape="\\"))


class _Stock:
    """Stock totals as SQL subqueries, joined only when a filter or sort needs them."""

    def __init__(self):
        totals = product_totals_query().subquery()
        issued = (select(GoodsIssueLine.reservation_id.label("rid"), func.sum(GoodsIssueLine.quantity).label("q"))
                  .join(GoodsIssue, GoodsIssue.id == GoodsIssueLine.issue_id)
                  .where(GoodsIssue.status == DocumentStatus.POSTED.value, GoodsIssueLine.reservation_id.is_not(None))
                  .group_by(GoodsIssueLine.reservation_id).subquery())
        left = StockReservation.quantity - func.coalesce(issued.c.q, 0)
        res = (select(StockReservation.product_id.label("pid"),
                      func.sum(case((left > 0, left), else_=0)).label("reserved"))
               .outerjoin(issued, issued.c.rid == StockReservation.id)
               .where(StockReservation.status == ReservationStatus.ACTIVE.value)
               .group_by(StockReservation.product_id).subquery())
        self.totals, self.res = totals, res
        self.on_hand = _q(func.coalesce(totals.c.on_hand, 0))
        self.usable = _q(func.coalesce(totals.c.available, 0))
        self.reserved = _q(func.coalesce(res.c.reserved, 0))
        self.available = _q(self.usable - self.reserved)
        level_set = and_(Product.reorder_level.is_not(None), Product.reorder_level > 0)
        min_set = and_(Product.min_stock.is_not(None), Product.min_stock > 0)
        self.reorder_point = case(
            (and_(level_set, or_(~min_set, Product.reorder_level > Product.min_stock)), Product.reorder_level),
            (min_set, Product.min_stock), else_=None)
        inc = _incoming_sq()
        self.inc = select(inc.c.pid, func.sum(inc.c.left).label("left")).group_by(inc.c.pid).subquery()
        self.incoming = _q(func.coalesce(self.inc.c.left, 0))

    def join(self, q):
        return (q.outerjoin(self.totals, self.totals.c.product_id == Product.id)
                .outerjoin(self.res, self.res.c.pid == Product.id)
                .outerjoin(self.inc, self.inc.c.pid == Product.id))


def _incoming_sq():
    """(pid, left) outstanding on open purchase orders, per order and product (as PurchasingService.incoming)."""
    received = (select(DeliveryNoteLine.purchase_order_line_id.label("lid"), func.sum(DeliveryNoteLine.quantity).label("q"))
                .join(DeliveryNote, DeliveryNote.id == DeliveryNoteLine.delivery_note_id)
                .where(DeliveryNote.status == DocumentStatus.POSTED.value,
                       DeliveryNoteLine.purchase_order_line_id.is_not(None))
                .group_by(DeliveryNoteLine.purchase_order_line_id).subquery())
    left = func.sum(PurchaseOrderLine.quantity - func.coalesce(received.c.q, 0))
    return (select(PurchaseOrderLine.product_id.label("pid"), left.label("left"))
            .join(PurchaseOrder, PurchaseOrder.id == PurchaseOrderLine.purchase_order_id)
            .outerjoin(received, received.c.lid == PurchaseOrderLine.id)
            .where(PurchaseOrder.status.in_(OPEN_PO_STATUSES))
            .group_by(PurchaseOrderLine.purchase_order_id, PurchaseOrderLine.product_id)
            .having(left > 0).subquery())


class _Project:
    """Required / Reserved / Issued / Returned for one project, as subqueries keyed by product."""

    def __init__(self, site_id: int):
        m = StockMovement
        go, rfs = MovementType.GOODS_OUT.value, MovementType.RETURN_FROM_SITE.value
        issued = func.sum(case((m.movement_type != go, 0), (m.from_bin_id.is_not(None), m.quantity), else_=-m.quantity))
        returned = func.sum(case((m.movement_type != rfs, 0), (m.to_bin_id.is_not(None), m.quantity), else_=-m.quantity))
        self.mov = (select(m.product_id.label("pid"), issued.label("issued"), returned.label("returned"))
                    .where(m.site_id == site_id, m.movement_type.in_((go, rfs)))
                    .group_by(m.product_id).subquery())
        self.req = (select(ProjectMaterial.product_id.label("pid"), ProjectMaterial.required_qty.label("required"))
                    .where(ProjectMaterial.site_id == site_id).subquery())
        gi_issued = (select(GoodsIssueLine.reservation_id.label("rid"), func.sum(GoodsIssueLine.quantity).label("q"))
                     .join(GoodsIssue, GoodsIssue.id == GoodsIssueLine.issue_id)
                     .where(GoodsIssue.status == DocumentStatus.POSTED.value, GoodsIssueLine.reservation_id.is_not(None))
                     .group_by(GoodsIssueLine.reservation_id).subquery())
        left = StockReservation.quantity - func.coalesce(gi_issued.c.q, 0)
        self.res = (select(StockReservation.product_id.label("pid"),
                           func.sum(case((and_(StockReservation.status == ReservationStatus.ACTIVE.value, left > 0), left),
                                         else_=0)).label("reserved"))
                    .outerjoin(gi_issued, gi_issued.c.rid == StockReservation.id)
                    .where(StockReservation.site_id == site_id,
                           StockReservation.status != ReservationStatus.RELEASED.value)
                    .group_by(StockReservation.product_id).subquery())
        self.required = _q(self.req.c.required)
        self.reserved = _q(func.coalesce(self.res.c.reserved, 0))
        self.issued = _q(func.coalesce(self.mov.c.issued, 0))
        self.returned = _q(func.coalesce(self.mov.c.returned, 0))
        self.net = _q(self.issued - self.returned)
        left = self.required - self.net - self.reserved
        self.remaining = _q(case((self.req.c.pid.is_(None), None), (left > 0, left), else_=0))

    def join(self, q):
        return (q.outerjoin(self.req, self.req.c.pid == Product.id)
                .outerjoin(self.res, self.res.c.pid == Product.id)
                .outerjoin(self.mov, self.mov.c.pid == Product.id))

    def associated(self):
        """A product belongs to the project if it is on its list, reserved for it or was issued /
        returned to it (spec section 4)."""
        return or_(self.req.c.pid.is_not(None), self.res.c.pid.is_not(None), self.mov.c.pid.is_not(None))


def project_product_ids(site_ids):
    """Products associated with any of the given projects (a select of site ids)."""
    m = StockMovement
    return (select(ProjectMaterial.product_id).where(ProjectMaterial.site_id.in_(site_ids))
            .union(select(StockReservation.product_id).where(StockReservation.site_id.in_(site_ids),
                                                             StockReservation.status != ReservationStatus.RELEASED.value))
            .union(select(m.product_id).where(m.site_id.in_(site_ids),
                                              m.movement_type.in_((MovementType.GOODS_OUT.value,
                                                                   MovementType.RETURN_FROM_SITE.value)))))


# --- the service ---------------------------------------------------------------------------------

class MaterialsService(BaseService):

    # ---- the one query ----------------------------------------------------------------------
    def _query(self, s: Session, f: MaterialFilter, columns=None, ordered: bool = True):
        """SELECT <columns> FROM products ... WHERE <every active filter> ORDER BY <sort>, id."""
        f.validate()
        # Stock totals cost a pass over every stock level: only join them when the WHERE clause
        # (a stock filter) or the ORDER BY (a stock sort, not needed for a count) uses them.
        st = _Stock() if f.stock or (ordered and f.sort in STOCK_SORTS) else None
        pj = _Project(f.site_id) if f.site_id else None
        q = select(*(columns or [Product.id])).select_from(Product)
        if st is not None:
            q = st.join(q)
        if pj is not None:
            q = pj.join(q)
        conds: list = []
        if f.text.strip():
            conds.append(material_text_filter(f.text))
        if f.status == "ACTIVE":
            conds.append(Product.is_active.is_(True))
        elif f.status == "ARCHIVED":
            conds.append(Product.is_active.is_(False))
        if pj is not None:
            conds.append(pj.associated())
        elif f.project_status:
            sites = select(Site.id).where(Site.status.in_(CLOSED_SITE_STATUSES) if f.project_status == "CLOSED"
                                          else Site.status.not_in(CLOSED_SITE_STATUSES))
            conds.append(Product.id.in_(project_product_ids(sites)))
        if f.supplier_id:
            conds.append(Product.supplier_id == f.supplier_id)
        if f.has_supplier == "WITH":
            conds.append(Product.supplier_id.is_not(None))
        elif f.has_supplier == "WITHOUT":
            conds.append(Product.supplier_id.is_(None))
        conds += self._bin_conditions(s, f)
        if f.category_id:
            conds.append(Product.category_id.in_(
                select(Category.id).where((Category.id == f.category_id) | (Category.parent_id == f.category_id))))
        if (f.brand or "").strip():
            conds.append(Product.brand.ilike(like_pattern(f.brand), escape="\\"))
        if f.photo in PRESENCE:
            has = exists().where(Attachment.entity_type == EntityType.PRODUCT.value, Attachment.entity_id == Product.id,
                                 Attachment.kind == AttachmentKind.PHOTO.value, Attachment.is_deleted.is_(False))
            conds.append(has if f.photo == "WITH" else ~has)
        if f.item_type == "FINISHED":
            conds.append(Product.item_type == ItemType.FINISHED.value)
        elif f.item_type == "RAW":
            conds.append(Product.item_type != ItemType.FINISHED.value)
        conds += self._date_conditions(f)
        if st is not None and f.stock:
            low = and_(st.reorder_point.is_not(None), st.available <= st.reorder_point)
            conds.append({"IN_STOCK": st.on_hand > 0, "OUT_OF_STOCK": st.on_hand <= 0, "LOW_STOCK": low,
                          "RESERVED": st.reserved > 0, "AVAILABLE": st.available > 0,
                          "INCOMING": st.incoming > 0}[f.stock])
        q = q.where(*conds)
        if ordered:
            q = q.order_by(*self._order(f, st, pj), Product.id)
        return q, st, pj

    def _bin_conditions(self, s: Session, f: MaterialFilter) -> list:
        conds = []
        held = and_(StockLevel.product_id == Product.id, StockLevel.quantity != ZERO)
        if f.bin_id:
            b = s.get(Bin, f.bin_id)
            if b is None:
                raise NotFoundError("The chosen bin no longer exists.")
            if b.is_active:
                conds.append(exists().where(held, StockLevel.bin_id == f.bin_id))
            else:
                # An archived bin holds nothing now; asking for it means its history (spec 10).
                m = StockMovement
                conds.append(Product.id.in_(select(m.product_id).where(
                    or_(m.from_bin_id == f.bin_id, m.to_bin_id == f.bin_id))))
        if f.bin_state == "NONE":
            conds.append(Product.default_bin_id.is_(None))
            conds.append(~exists().where(held))
        elif f.bin_state == "MULTIPLE":
            conds.append(select(func.count(StockLevel.id)).where(held).scalar_subquery() >= 2)
        elif f.bin_state == "ACTIVE":
            conds.append(exists().where(held, StockLevel.bin_id.in_(select(Bin.id).where(Bin.is_active.is_(True)))))
        if f.warehouse_id:
            loc = s.get(Location, f.warehouse_id)
            if loc is None:
                raise NotFoundError("The chosen location no longer exists.")
            bins = select(Bin.id).where(or_(Bin.full_code.like(prefix_pattern(loc.path), escape="\\"),
                                            Bin.location_id == loc.id))
            conds.append(exists().where(held, StockLevel.bin_id.in_(bins)))
        return conds

    @staticmethod
    def _date_conditions(f: MaterialFilter) -> list:
        conds = []
        m = StockMovement
        for kind, lo, hi in ((MovementType.GOODS_IN.value, f.received_from, f.received_to),
                             (MovementType.GOODS_OUT.value, f.issued_from, f.issued_to)):
            if lo or hi:
                c = [m.product_id == Product.id, m.movement_type == kind, m.reversal_of_id.is_(None)]
                if lo:
                    c.append(m.occurred_at >= _start(lo))
                if hi:
                    c.append(m.occurred_at < _end(hi))
                conds.append(exists().where(*c))
        for col, lo, hi in ((Product.created_at, f.created_from, f.created_to),
                            (Product.updated_at, f.updated_from, f.updated_to)):
            if lo:
                conds.append(col >= _start(lo))
            if hi:
                conds.append(col < _end(hi))
        return conds

    @staticmethod
    def _last(kind: str):
        m = StockMovement
        return (select(func.max(m.occurred_at))
                .where(m.product_id == Product.id, m.movement_type == kind, m.reversal_of_id.is_(None))
                .scalar_subquery())

    def _order(self, f: MaterialFilter, st: _Stock | None, pj: _Project | None) -> list:
        key = f.sort
        if key in STOCK_SORTS and st is not None:
            status = case((Product.is_active.is_(False), 4), (st.on_hand <= 0, 0),
                          (and_(st.reorder_point.is_not(None), st.available <= st.reorder_point), 1), else_=2)
            col = {"qty": st.on_hand, "available": st.available, "reserved": st.reserved,
                   "incoming": st.incoming, "status": status}[key]
        elif key in PROJECT_SORTS and pj is not None:
            col = {"required": pj.required, "proj_reserved": pj.reserved, "issued": pj.issued,
                   "returned": pj.returned, "net": pj.net, "remaining": pj.remaining}[key]
        else:
            col = {
                "name": func.lower(Product.name),
                "product_no": func.lower(Product.supplier_code),
                "supplier": select(func.lower(Supplier.name)).where(Supplier.id == Product.supplier_id).scalar_subquery(),
                "category": select(func.lower(Category.name)).where(Category.id == Product.category_id).scalar_subquery(),
                "brand": func.lower(Product.brand),
                "bin": select(func.min(Bin.code)).join(StockLevel, StockLevel.bin_id == Bin.id).where(
                    StockLevel.product_id == Product.id, StockLevel.quantity != ZERO).scalar_subquery(),
                "received": self._last(MovementType.GOODS_IN.value),
                "issued_date": self._last(MovementType.GOODS_OUT.value),
                "updated": Product.updated_at,
                "created": Product.created_at,
            }.get(key, Product.internal_code)
        # Empty values last in both directions, as people expect.
        return [col.is_(None), col.desc() if f.descending else col.asc()]

    # ---- reading ----------------------------------------------------------------------------
    def count(self, f: MaterialFilter) -> int:
        self.require(P.PRODUCTS_VIEW)
        with self.db.read_session() as s:
            q, _, _ = self._query(s, f, ordered=False)
            return s.scalar(select(func.count()).select_from(q.subquery())) or 0

    def page(self, f: MaterialFilter, offset: int = 0, limit: int = PAGE_SIZE) -> list[MaterialRow]:
        """Rows ``offset`` .. ``offset + limit`` of the list, in display order."""
        self.require(P.PRODUCTS_VIEW)
        with self.db.read_session() as s:
            q, _, _ = self._query(s, f)
            ids = list(s.scalars(q.offset(max(offset, 0)).limit(max(limit, 0))))
            return self._rows(s, ids, f.site_id)

    def ids(self, f: MaterialFilter) -> list[int]:
        """Every matching product id in display order (for "select all")."""
        self.require(P.PRODUCTS_VIEW)
        with self.db.read_session() as s:
            q, _, _ = self._query(s, f)
            return list(s.scalars(q))

    def iter_rows(self, f: MaterialFilter, chunk: int = 1000) -> Iterator[list[MaterialRow]]:
        """Every matching row, in display order, ``chunk`` rows at a time (Excel export)."""
        self.require(P.PRODUCTS_VIEW)
        with self.db.read_session() as s:
            q, _, _ = self._query(s, f)
            ids = list(s.scalars(q))
            for i in range(0, len(ids), chunk):
                yield self._rows(s, ids[i:i + chunk], f.site_id)

    def rows_for(self, product_ids: list[int], site_id: int | None = None) -> list[MaterialRow]:
        """The given products, in the given order (Export Selected)."""
        self.require(P.PRODUCTS_VIEW)
        out: list[MaterialRow] = []
        with self.db.read_session() as s:
            for i in range(0, len(product_ids), 1000):
                out += self._rows(s, product_ids[i:i + 1000], site_id)
        return out

    def _rows(self, s: Session, ids: list[int], site_id: int | None) -> list[MaterialRow]:
        """Full rows for the given ids: a fixed number of grouped queries, whatever the count."""
        if not ids:
            return []
        products = {p.id: p for p in s.scalars(
            select(Product).where(Product.id.in_(ids)).options(
                selectinload(Product.supplier), selectinload(Product.category), selectinload(Product.unit),
                selectinload(Product.default_bin))).unique()}
        rows = {i: MaterialRow(products[i]) for i in ids if i in products}
        for pid, oh, us in s.execute(product_totals_query().where(StockLevel.product_id.in_(ids))):
            rows[pid].on_hand, rows[pid].usable = Decimal(oh or 0), Decimal(us or 0)
        st = _Stock()
        for pid, rs in s.execute(select(st.res.c.pid, st.res.c.reserved).where(st.res.c.pid.in_(ids))):
            rows[pid].reserved = _q_value(rs)
        inc = _incoming_sq()
        for pid, left in s.execute(select(inc.c.pid, func.sum(inc.c.left)).where(inc.c.pid.in_(ids))
                                   .group_by(inc.c.pid)):
            rows[pid].incoming = _q_value(left)
        for pid, b, qty in s.execute(
                select(StockLevel.product_id, Bin, StockLevel.quantity).join(Bin, Bin.id == StockLevel.bin_id)
                .where(StockLevel.product_id.in_(ids), StockLevel.quantity != ZERO)
                .order_by(StockLevel.quantity.desc(), Bin.code)).unique():
            rows[pid].bins.append(BinStock(b.code, b.full_code, qty, b.is_active))
        # the main photo (primary first, then the oldest) - one query for the whole page
        for pid, att_id, stored in s.execute(
                select(Attachment.entity_id, Attachment.id, Attachment.stored_name)
                .where(Attachment.entity_type == EntityType.PRODUCT.value, Attachment.entity_id.in_(ids),
                       Attachment.kind == AttachmentKind.PHOTO.value, Attachment.is_deleted.is_(False))
                .order_by(Attachment.is_primary.desc(), Attachment.id)):
            if rows[pid].photo_id is None:
                rows[pid].photo_id, rows[pid].photo_name = att_id, stored
        if site_id:
            pj = _Project(site_id)
            for pid, req in s.execute(select(pj.req.c.pid, pj.req.c.required).where(pj.req.c.pid.in_(ids))):
                rows[pid].required, rows[pid].on_list = _q_value(req), True
            for pid, rs in s.execute(select(pj.res.c.pid, pj.res.c.reserved).where(pj.res.c.pid.in_(ids))):
                rows[pid].proj_reserved = _q_value(rs)
            for pid, iss, ret in s.execute(select(pj.mov.c.pid, pj.mov.c.issued, pj.mov.c.returned)
                                           .where(pj.mov.c.pid.in_(ids))):
                rows[pid].issued, rows[pid].returned = _q_value(iss), _q_value(ret)
        return [rows[i] for i in ids if i in rows]

    def project_summary(self, site_id: int) -> ProjectSummary:
        self.require(P.SITES_VIEW)
        with self.db.read_session() as s:
            site = s.get(Site, site_id)
            if site is None:
                raise NotFoundError("Project not found.")
            pj = _Project(site_id)
            q = pj.join(select(func.count(Product.id), func.sum(pj.required), func.sum(pj.reserved),
                               func.sum(pj.issued), func.sum(pj.returned), func.sum(pj.remaining))
                        .select_from(Product)).where(pj.associated())
            n, req, res, iss, ret, rem = s.execute(q).one()
            return ProjectSummary(site, n or 0, _q_value(req), _q_value(res), _q_value(iss), _q_value(ret),
                                  _q_value(rem))

    # ---- choices for the filter drop-downs ------------------------------------------------------
    def bin_choices(self, include_archived: bool = False) -> list[Bin]:
        q = select(Bin).order_by(Bin.full_code)
        if not include_archived:
            q = q.where(Bin.is_active.is_(True))
        with self.db.read_session() as s:
            return list(s.scalars(q).unique())

    def brands(self) -> list[str]:
        with self.db.read_session() as s:
            return [b for b in s.scalars(select(Product.brand).where(Product.brand.is_not(None))
                                         .distinct().order_by(func.lower(Product.brand))) if b.strip()]

    def describe(self, f: MaterialFilter) -> list[tuple[str, str]]:
        """(field, chip text) for every active filter, in the order the chips are shown."""
        out: list[tuple[str, str]] = []
        with self.db.read_session() as s:
            def name(model, oid, attr="name"):
                o = s.get(model, oid)
                return getattr(o, attr) if o is not None else f"#{oid}"
            if f.site_id:
                out.append(("site_id", f"Project: {name(Site, f.site_id)}"))
            if f.supplier_id:
                out.append(("supplier_id", f"Supplier: {name(Supplier, f.supplier_id)}"))
            if f.bin_id:
                b = s.get(Bin, f.bin_id)
                out.append(("bin_id", f"Bin: {b.code if b else f.bin_id}{'' if b is None or b.is_active else ' (archived)'}"))
            if f.bin_state:
                out.append(("bin_state", BIN_STATES[f.bin_state]))
            if f.stock:
                out.append(("stock", STOCK_CHOICES[f.stock]))
            if f.text.strip():
                out.append(("text", f"Search: {f.text.strip()}"))
            if f.category_id:
                out.append(("category_id", f"Category: {name(Category, f.category_id)}"))
            if (f.brand or "").strip():
                out.append(("brand", f"Brand: {f.brand.strip()}"))
            if f.warehouse_id:
                out.append(("warehouse_id", f"Location: {name(Location, f.warehouse_id, 'path')}"))
        if f.project_status:
            out.append(("project_status", PROJECT_STATUS_CHOICES.get(f.project_status, f.project_status)))
        if f.status != "ACTIVE":
            out.append(("status", STATUS_CHOICES[f.status]))
        if f.photo:
            out.append(("photo", "Has Photo" if f.photo == "WITH" else "Missing Photo"))
        if f.has_supplier:
            out.append(("has_supplier", "Has Supplier" if f.has_supplier == "WITH" else "Missing Supplier"))
        if f.item_type:
            out.append(("item_type", ITEM_TYPES[f.item_type]))
        for key, label in (("received", "Received"), ("issued", "Issued"), ("created", "Created"),
                           ("updated", "Updated")):
            lo, hi = getattr(f, f"{key}_from"), getattr(f, f"{key}_to")
            if lo or hi:
                text = f"{lo:%d/%m/%Y}" if lo else "…"
                text += f" – {hi:%d/%m/%Y}" if hi else " onwards"
                out.append((key, f"{label}: {text}"))
        return out

    @staticmethod
    def clear(f: MaterialFilter, key: str) -> None:
        """Remove one filter (the × on its chip)."""
        if key in ("received", "issued", "created", "updated"):
            setattr(f, f"{key}_from", None)
            setattr(f, f"{key}_to", None)
        elif key == "text":
            f.text = ""
        elif key == "status":
            f.status = "ACTIVE"
        else:
            setattr(f, key, None)
        if key == "site_id" and f.sort in PROJECT_SORTS:
            f.sort, f.descending = "code", False

    # ---- saved views (spec section 41) -------------------------------------------------------
    def views(self) -> list[SavedView]:
        out = []
        for name, data in BUILTIN_VIEWS:
            data = dict(data)
            if data.get("received_from") == "__recent__":
                data["received_from"] = (date.today() - timedelta(days=RECENT_DAYS - 1)).isoformat()
            out.append(SavedView(None, name, MaterialFilter.from_dict(data)))
        with self.db.read_session() as s:
            for v in s.scalars(select(SavedSearch).where(SavedSearch.user_id == self.user.id,
                                                         SavedSearch.criteria.startswith(VIEW_PREFIX))
                               .order_by(func.lower(SavedSearch.name))):
                try:
                    data = json.loads(v.criteria)["materials"]
                except (ValueError, KeyError, TypeError):
                    continue
                out.append(SavedView(v.id, v.name, MaterialFilter.from_dict(data)))
        return out

    def save_view(self, name: str, f: MaterialFilter, replace: bool = False) -> SavedView:
        name = clean_str(name, "name", required=True, max_len=80)
        if any(name.lower() == b.lower() for b, _ in BUILTIN_VIEWS):
            raise ValidationError(f"'{name}' is a built-in view. Choose another name.", "name")
        f.validate()
        text = json.dumps({"materials": f.to_dict()}, sort_keys=True)
        with self.db.session() as s:
            existing = s.scalar(select(SavedSearch).where(SavedSearch.user_id == self.user.id,
                                                          func.lower(SavedSearch.name) == name.lower()))
            if existing is not None:
                if not replace:
                    raise ValidationError(f"You already have a view called '{existing.name}'.", "name")
                existing.criteria, existing.name = text, name
                saved = existing
            else:
                saved = SavedSearch(user_id=self.user.id, name=name, criteria=text)
                s.add(saved)
            s.flush()
            return SavedView(saved.id, saved.name, f.copy())

    def delete_view(self, view_id: int) -> None:
        with self.db.session() as s:
            s.execute(delete(SavedSearch).where(SavedSearch.id == view_id, SavedSearch.user_id == self.user.id,
                                                SavedSearch.criteria.startswith(VIEW_PREFIX)))

    # ---- bulk changes (contextual toolbar, spec section 18) -----------------------------------
    def bulk_set(self, product_ids: list[int], field_name: str, value: int | None) -> int:
        """Change the supplier or category of many materials at once. Master data only."""
        self.require(P.PRODUCTS_EDIT)
        if field_name not in ("supplier_id", "category_id"):
            raise ValidationError("Only the supplier or the category can be changed for many materials at once.")
        ids = sorted(set(product_ids))
        if not ids:
            raise ValidationError("Select at least one material.")
        model = Supplier if field_name == "supplier_id" else Category
        with self.db.session() as s:
            target = s.get(model, value) if value is not None else None
            if value is not None and target is None:
                raise NotFoundError("The chosen supplier / category no longer exists.")
            if target is not None and not target.is_active:
                raise ValidationError(f"{target.name} is archived. Restore it first or choose another.")
            changed = 0
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                old = dict(s.execute(select(Product.id, getattr(Product, field_name)).where(Product.id.in_(chunk))).all())
                todo = [pid for pid, v in old.items() if v != value]
                if todo:
                    s.execute(update(Product).where(Product.id.in_(todo))
                              .values({field_name: value, "updated_at": utcnow(), "version": Product.version + 1}))
                    changed += len(todo)
            what = "supplier" if field_name == "supplier_id" else "category"
            audit.record(s, self.ctx, "PRODUCTS_BULK_UPDATED", EntityType.PRODUCT.value, None,
                         f"{changed} materials",
                         f"{what.capitalize()} set to {target.name if target else '(none)'} on {changed} of "
                         f"{len(ids)} selected materials", new={field_name: value, "products": ids[:500]})
        return changed

    # ---- project material lists (spec section 7) ----------------------------------------------
    def set_required(self, site_id: int, items: list[tuple[int, object]], notes: str | None = None,
                     replace: bool = True) -> int:
        """Put materials on a project's list with their required quantity. ``replace`` sets the
        quantity; otherwise it is added to what is already required. Never changes stock."""
        self.require(P.SITES_EDIT)
        if not items:
            raise ValidationError("Choose at least one material.")
        with self.db.session() as s:
            n = upsert_project_materials(s, self.ctx, site_id, items, notes=notes, replace=replace)
        return n

    def remove_from_project(self, site_id: int, product_ids: list[int]) -> int:
        """Take materials off a project's list. Reservations and issued stock are untouched."""
        self.require(P.SITES_EDIT)
        with self.db.session() as s:
            res = s.execute(delete(ProjectMaterial).where(ProjectMaterial.site_id == site_id,
                                                          ProjectMaterial.product_id.in_(product_ids)))
            site = s.get(Site, site_id)
            audit.record(s, self.ctx, "PROJECT_MATERIALS_REMOVED", EntityType.SITE.value, site_id,
                         site.project_number if site else None,
                         f"{res.rowcount} material(s) taken off the project list",
                         old={"products": sorted(product_ids)[:500]})
            return res.rowcount or 0


def _q_value(v) -> Decimal:
    """A Quantity aggregate read through a subquery column (stored as thousandths)."""
    if v is None:
        return ZERO
    if isinstance(v, Decimal):
        return v
    return Quantity().process_result_value(v, None)


def upsert_project_materials(s: Session, ctx, site_id: int, items: list[tuple[int, object]],
                             notes: str | None = None, replace: bool = True, source: str = "manual") -> int:
    """Create / update project material rows inside the caller's transaction (used by the screen
    and by the Excel import). Returns the number of rows written."""
    site = s.get(Site, site_id)
    if site is None:
        raise NotFoundError("Project not found.")
    if site.status in CLOSED_SITE_STATUSES:
        raise ValidationError(f"Project {site.project_number} is {site.status.lower().replace('_', ' ')}: "
                              f"its material list can no longer be changed.")
    merged: dict[int, Decimal] = {}
    for pid, qty in items:
        q = to_qty(qty if qty not in (None, "") else 0, "required quantity")
        if q < 0:
            raise ValidationError("Required quantity cannot be negative.")
        merged[pid] = merged.get(pid, ZERO) + q
    existing = {pm.product_id: pm for pm in s.scalars(
        select(ProjectMaterial).where(ProjectMaterial.site_id == site_id, ProjectMaterial.product_id.in_(merged)))}
    found = set(s.scalars(select(Product.id).where(Product.id.in_(merged))))
    missing = set(merged) - found
    if missing:
        raise NotFoundError(f"{len(missing)} material(s) no longer exist.")
    user_id = ctx.user.id if ctx.user else None
    changes = []
    for pid, q in merged.items():
        pm = existing.get(pid)
        if pm is None:
            s.add(ProjectMaterial(site_id=site_id, product_id=pid, required_qty=q, notes=clean_str(notes),
                                  created_by_id=user_id))
            changes.append([pid, None, str(q)])
        else:
            new = q if replace else pm.required_qty + q
            if new != pm.required_qty:
                changes.append([pid, str(pm.required_qty), str(new)])
                pm.required_qty = new
            if notes:
                pm.notes = clean_str(notes)
    s.flush()
    if changes:
        audit.record(s, ctx, "PROJECT_MATERIALS_SET", EntityType.SITE.value, site_id, site.project_number,
                     f"{len(changes)} required quantit{'y' if len(changes) == 1 else 'ies'} set ({source})",
                     new={"changes": changes[:500]})
    return len(changes)


__all__ = ["BIN_STATES", "BUILTIN_VIEWS", "ITEM_TYPES", "PAGE_SIZE", "PRESENCE", "PROJECT_STATUS_CHOICES",
           "SORT_KEYS", "STATUS_CHOICES", "STOCK_CHOICES", "MaterialFilter", "MaterialRow", "MaterialsService",
           "ProjectSummary", "SavedView", "material_text_filter", "upsert_project_materials"]
