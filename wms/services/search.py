"""Warehouse search: one search box over everything, advanced filters that combine, and the
dedicated views a result opens (bin, project, supplier, product history).

Design:

* ``SearchCriteria`` is a plain, JSON-serialisable description of a search (text + filters +
  sort). The screen builds it, saved searches store it, and the service turns it into SQL.
  Adding a filter later means one new field here and one condition below - no schema change.
* Two result lists share the same criteria: **products** (what we have and where) and
  **transactions** (what happened: the stock movement ledger). Every filter is applied in SQL,
  every filter is AND-ed with the others, and the count is a real COUNT, not "rows shown".
* A barcode scan is just a search: an exact match on code / barcode is detected and offered
  for one-click Goods In / Goods Out / Transfer. There is no second search path for scanners.
* Everything is filtered by ids (supplier_id, site_id, bin_id...). Names are only looked up
  for display, so renaming or archiving a supplier, project or bin never breaks a filter or
  hides history.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, fields
from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import and_, case, delete, exists, func, or_, select, type_coerce
from sqlalchemy.orm import selectinload

from wms.core.enums import (
    CLOSED_SITE_STATUSES,
    AttachmentKind,
    EntityType,
    ItemType,
    LocationKind,
    MovementType,
    POStatus,
    ReservationStatus,
    SiteStatus,
)
from wms.core.exceptions import NotFoundError, ValidationError
from wms.core.numbers import ZERO, fmt_date, fmt_qty, utcnow
from wms.core.permissions import P
from wms.db.models import (
    Attachment,
    Bin,
    Category,
    DeliveryDiscrepancy,
    DeliveryNote,
    DeliveryNoteLine,
    GoodsIssue,
    Location,
    Product,
    PurchaseOrder,
    PurchaseOrderLine,
    RecentSearch,
    SavedSearch,
    Site,
    StockLevel,
    StockMovement,
    StockReservation,
    StockReturn,
    Supplier,
    Unit,
    User,
)
from wms.db.types import Quantity, UTCDateTime
from wms.services.base import BaseService, clean_str, like_pattern, prefix_pattern
from wms.services.goods_in import normalise_dn_number
from wms.services.lifecycle import last_removal
from wms.services.products import (
    ProductRow,
    code_key,
    code_key_sql,
    key_pattern,
    search_terms,
    text_filter,
)
from wms.services.reservations import reserved_query
from wms.services.stock_engine import product_totals_query

# --- vocabularies ------------------------------------------------------------------

#: Transaction filter groups -> movement types in the ledger.
MOVEMENT_GROUPS: dict[str, tuple[str, tuple[str, ...]]] = {
    "GOODS_IN": ("Goods In", (MovementType.GOODS_IN.value,)),
    "GOODS_OUT": ("Goods Out", (MovementType.GOODS_OUT.value,)),
    "TRANSFER": ("Transfer", (MovementType.TRANSFER.value,)),
    "RETURN": ("Return", (MovementType.RETURN_FROM_SITE.value, MovementType.RETURN_TO_SUPPLIER.value)),
    "ADJUSTMENT": ("Adjustment", (MovementType.ADJUSTMENT_IN.value, MovementType.ADJUSTMENT_OUT.value,
                                  MovementType.OPENING_BALANCE.value,
                                  MovementType.STOCKTAKE_ADJUSTMENT.value)),
    "PRODUCTION": ("Production", (MovementType.PRODUCTION_OUTPUT.value, MovementType.PRODUCTION_CONSUME.value)),
    # Reversals keep their original type in the ledger; this group selects them by their link.
    "REVERSAL": ("Reversal", ()),
}

DATE_PRESETS = {
    "TODAY": "Today",
    "YESTERDAY": "Yesterday",
    "LAST_7": "Last 7 Days",
    "LAST_30": "Last 30 Days",
    "THIS_MONTH": "This Month",
    "CUSTOM": "Custom Date Range",
}

STOCK_STATUSES = {"IN_STOCK": "In Stock", "OUT_OF_STOCK": "Out of Stock", "LOW_STOCK": "Reorder Required"}

QUICK_FILTERS = {
    "ALL": "All",
    "IN_STOCK": "In Stock",
    "LOW_STOCK": "Reorder Required",
    "OUT_OF_STOCK": "Out of Stock",
    "RECENTLY_RECEIVED": "Recently Received",
    "RECENTLY_ISSUED": "Recently Issued",
    "FINISHED": "Finished Products",
    "RAW": "Raw Materials",
}
RECENT_DAYS = 7  # "Recently Received / Issued" = the last 7 days

ARCHIVE_CHOICES = {"ACTIVE": "Active", "ARCHIVED": "Archived", "ALL": "Active and Archived"}
PROJECT_STATUS_CHOICES = {"ACTIVE": "Active", "CLOSED": "Closed"}
#: Historical names kept on every movement - searched alongside the current names.
SNAPSHOT_TEXT_COLUMNS = (
    StockMovement.product_code_snapshot, StockMovement.product_name_snapshot,
    StockMovement.from_bin_name_snapshot, StockMovement.to_bin_name_snapshot,
    StockMovement.site_number_snapshot, StockMovement.site_name_snapshot,
    StockMovement.supplier_name_snapshot, StockMovement.user_name_snapshot,
)
ATTACHMENT_CHOICES = {"WITH": "Has Attachment", "WITHOUT": "Missing Attachment"}
PHOTO_CHOICES = {"WITH": "Has Photo", "WITHOUT": "Missing Photo"}
DEFAULT_BIN_CHOICES = {"WITH": "Bin Assigned", "WITHOUT": "No Bin Assigned"}

LOCATION_LEVELS = (("warehouse_id", LocationKind.WAREHOUSE), ("zone_id", LocationKind.ZONE),
                   ("rack_id", LocationKind.RACK), ("shelf_id", LocationKind.SHELF))

MAX_RECENT = 10
MAX_SAVED = 100

#: Chip label for each filter field ("Supplier: Egger x").
FILTER_LABELS = {
    "product_code": "Code",
    "supplier_code": "Supplier Code",
    "barcode": "Barcode",
    "name": "Product",
    "description": "Description",
    "category_id": "Category",
    "brand": "Brand",
    "archive": "Status",
    "photo": "Photo",
    "default_bin": "Default Bin",
    "stock_status": "Stock",
    "qty_gt": "Qty more than",
    "qty_lt": "Qty less than",
    "warehouse_id": "Warehouse",
    "zone_id": "Zone",
    "rack_id": "Rack",
    "shelf_id": "Shelf",
    "bin_id": "Bin",
    "supplier_id": "Supplier",
    "site_id": "Project",
    "project_number": "Project No.",
    "customer": "Customer",
    "project_manager": "Project Manager",
    "project_status": "Project",
    "movement_types": "Transaction",
    "dn_number": "Delivery Note",
    "po_number": "Purchase Order",
    "reference": "Reference",
    "attachment": "Attachment",
    "date": "Date",
}


# --- criteria ----------------------------------------------------------------------

@dataclass
class SearchCriteria:
    """Everything that defines one search. Empty / None fields are "no filter"."""

    text: str = ""
    # Product
    product_code: str | None = None
    supplier_code: str | None = None
    barcode: str | None = None
    name: str | None = None
    description: str | None = None
    category_id: int | None = None
    brand: str | None = None
    archive: str | None = None  # ACTIVE / ARCHIVED / ALL; None = products: active only, transactions: all
    photo: str | None = None  # WITH / WITHOUT a product photo
    default_bin: str | None = None  # WITH / WITHOUT a default storage bin
    # Stock
    stock_status: str | None = None
    qty_gt: Decimal | None = None
    qty_lt: Decimal | None = None
    # Location (each is a location id at that level)
    warehouse_id: int | None = None
    zone_id: int | None = None
    rack_id: int | None = None
    shelf_id: int | None = None
    bin_id: int | None = None
    # Supplier
    supplier_id: int | None = None
    # Project
    site_id: int | None = None
    project_number: str | None = None
    customer: str | None = None
    project_manager: str | None = None
    project_status: str | None = None
    # Transactions / documents
    movement_types: list[str] = field(default_factory=list)  # MOVEMENT_GROUPS keys
    dn_number: str | None = None
    po_number: str | None = None
    reference: str | None = None
    attachment: str | None = None
    # Date
    date_preset: str | None = None
    date_from: date | None = None
    date_to: date | None = None
    # One-click filter above the table
    quick: str = "ALL"

    # -- (de)serialisation for saved searches -------------------------------
    def to_dict(self) -> dict:
        out = {}
        for f in fields(self):
            v = getattr(self, f.name)
            if v in (None, "", []) or (f.name == "quick" and v == "ALL"):
                continue
            if isinstance(v, Decimal):
                v = format(v.normalize(), "f")
            elif isinstance(v, date):
                v = v.isoformat()
            out[f.name] = v
        return out

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True)

    @classmethod
    def from_dict(cls, data: dict) -> SearchCriteria:
        known = {f.name for f in fields(cls)}
        c = cls()
        for k, v in (data or {}).items():
            if k not in known or v is None:
                continue  # a saved search from a newer/older version: ignore what we don't know
            if k in ("qty_gt", "qty_lt"):
                v = Decimal(str(v))
            elif k in ("date_from", "date_to"):
                v = date.fromisoformat(v)
            elif k == "movement_types":
                v = [x for x in v if x in MOVEMENT_GROUPS]
            setattr(c, k, v)
        return c

    @classmethod
    def from_json(cls, text: str) -> SearchCriteria:
        return cls.from_dict(json.loads(text or "{}"))

    def copy(self) -> SearchCriteria:
        return SearchCriteria.from_dict(self.to_dict())

    # -- filters as chips -----------------------------------------------------
    def active_keys(self) -> list[str]:
        """Filter fields that are set, in display order (``text`` and ``quick`` are not chips)."""
        keys = []
        for k in FILTER_LABELS:
            if k == "date":
                if self.date_preset or self.date_from or self.date_to:
                    keys.append(k)
            elif getattr(self, k) not in (None, "", []):
                keys.append(k)
        return keys

    def clear(self, key: str) -> None:
        """Remove one filter (the x on its chip)."""
        if key == "date":
            self.date_preset = self.date_from = self.date_to = None
        elif key == "movement_types":
            self.movement_types = []
        elif key == "text":
            self.text = ""
        elif key == "quick":
            self.quick = "ALL"
        else:
            setattr(self, key, None)

    def clear_filters(self) -> None:
        """Clear All Filters: everything except the search text."""
        text = self.text
        self.__init__()
        self.text = text

    @property
    def has_filters(self) -> bool:
        return bool(self.active_keys()) or self.quick != "ALL"

    # -- dates --------------------------------------------------------------
    def date_range(self, today: date | None = None) -> tuple[date | None, date | None]:
        today = today or date.today()
        p = self.date_preset
        if p == "TODAY":
            return today, today
        if p == "YESTERDAY":
            y = today - timedelta(days=1)
            return y, y
        if p == "LAST_7":
            return today - timedelta(days=6), today
        if p == "LAST_30":
            return today - timedelta(days=29), today
        if p == "THIS_MONTH":
            return today.replace(day=1), today
        return self.date_from, self.date_to

    def validate(self) -> None:
        f, t = self.date_range()
        if f and t and t < f:
            raise ValidationError("The 'to' date is before the 'from' date.", "date_to")
        if self.qty_gt is not None and self.qty_lt is not None and self.qty_lt <= self.qty_gt:
            raise ValidationError("'Quantity less than' must be bigger than 'Quantity greater than'.", "qty_lt")


# --- results -----------------------------------------------------------------------

@dataclass
class BinQty:
    bin: Bin
    quantity: Decimal


@dataclass
class ProductHit(ProductRow):
    """A product result: the product, its totals and every bin holding it."""

    bins: list[BinQty] = field(default_factory=list)

    @property
    def id(self) -> int:  # lets tables keep the selection across refreshes
        return self.product.id

    @property
    def location_text(self) -> str:
        if not self.bins:
            b = self.product.default_bin
            return f"{location_of(b)} (default)" if b else ""
        locs = {location_of(b.bin) for b in self.bins}
        return locs.pop() if len(locs) == 1 else f"{len(locs)} locations"

    @property
    def bin_text(self) -> str:
        if not self.bins:
            return ""
        first = self.bins[0].bin.code
        return first if len(self.bins) == 1 else f"{first} +{len(self.bins) - 1} more"


@dataclass
class SearchResult:
    rows: list
    total: int
    limit: int
    exact: Product | None = None  # the product a scanned / typed code identifies exactly

    @property
    def truncated(self) -> bool:
        return self.total > len(self.rows)

    def summary(self, noun: str = "result") -> str:
        if self.total == 0:
            return "No matching records found"
        text = f"{self.total:,} {noun}{'' if self.total == 1 else 's'}"
        if self.truncated:
            text += f"  (showing the first {len(self.rows):,} - add a filter to narrow down)"
        return text


@dataclass
class SearchHit:
    kind: str  # Product, Delivery Note, Goods Out, Site / Project, Bin, Supplier, Return, Movement
    entity_type: str
    entity_id: int
    title: str
    detail: str

    @property
    def id(self) -> tuple[str, int]:
        return self.entity_type, self.entity_id


@dataclass
class BinView:
    bin: Bin
    path: list[tuple[str, str]]  # [("Warehouse", "Main Warehouse"), ("Zone", "Zone B"), ...]
    contents: list[StockLevel]
    recent_in: list[StockMovement]
    recent_out: list[StockMovement]
    history: list[StockMovement] = field(default_factory=list)  # every movement in or out, newest first
    history_count: int = 0
    removed_on: datetime | None = None  # when it was taken out of active use (archived bins)
    removed_by: str | None = None
    removed_reason: str | None = None

    @property
    def is_archived(self) -> bool:
        return not self.bin.is_active

    @property
    def current_stock(self) -> Decimal:
        return sum((lvl.quantity for lvl in self.contents), ZERO)

    @property
    def product_count(self) -> int:
        return len({lvl.product_id for lvl in self.contents})

    @property
    def path_text(self) -> str:
        return " → ".join(name for _kind, name in self.path)


@dataclass
class SupplierProductRow:
    product: Product
    received: Decimal
    deliveries: int
    last_received: datetime | None

    @property
    def id(self) -> int:
        return self.product.id


@dataclass
class SupplierView:
    supplier: Supplier
    products: list[SupplierProductRow]
    delivery_count: int
    last_delivery: date | None
    total_received_lines: int

    @property
    def products_supplied(self) -> int:
        return len(self.products)


# --- helpers used by several screens -------------------------------------------------

def location_of(b: Bin | None) -> str:
    """Location part of a bin's full code: 'MAIN / A / R03 / S02 / B04' -> 'MAIN / A / R03 / S02'."""
    if b is None:
        return ""
    full = b.full_code
    suffix = " / " + b.code
    return full[: -len(suffix)] if full.endswith(suffix) else full


def signed_qty(m: StockMovement) -> Decimal:
    """+ into the warehouse, - out of it; a bin-to-bin transfer stays positive."""
    if m.to_bin_id and not m.from_bin_id:
        return m.quantity
    if m.from_bin_id and not m.to_bin_id:
        return -m.quantity
    return m.quantity


def movement_route(m: StockMovement) -> tuple[str, str]:
    """Plain-language From / To: 'Egger -> Bin B01', 'Bin A03 -> Surrey County Hall'."""
    label = MovementType(m.movement_type).label

    def outside() -> str:
        return m.site_label or m.supplier_label or label

    # Names as they were when the movement happened (a bin removed or renamed since still shows).
    src = f"Bin {m.from_bin_code}" if m.from_bin_id is not None else outside()
    dst = f"Bin {m.to_bin_code}" if m.to_bin_id is not None else outside()
    return src, dst


def movement_reference(m: StockMovement) -> str:
    parts = [p for p in (m.doc_number, m.reference) if p]
    if len(parts) == 2 and parts[0] == parts[1]:
        parts = parts[:1]
    return " / ".join(parts)


def _start(d: date) -> datetime:
    return datetime.combine(d, datetime.min.time()).astimezone()


def _end(d: date) -> datetime:
    return datetime.combine(d, datetime.max.time()).astimezone()


def _ilike(col, text: str):
    return col.ilike(like_pattern(text), escape="\\")


class StockColumns:
    """SQL for a product's stock states (see ProductRow), joined onto a product query with join()."""

    def __init__(self):
        self.totals = product_totals_query().subquery()
        self.res = reserved_query().subquery()
        self.on_hand = type_coerce(func.coalesce(self.totals.c.on_hand, 0), Quantity())
        self.usable = type_coerce(func.coalesce(self.totals.c.available, 0), Quantity())
        self.reserved = type_coerce(func.coalesce(self.res.c.reserved, 0), Quantity())
        self.available = type_coerce(self.usable - self.reserved, Quantity())
        # The reorder level when set and above the minimum, else the minimum; NULL = never flagged.
        level_set = and_(Product.reorder_level.is_not(None), Product.reorder_level > 0)
        min_set = and_(Product.min_stock.is_not(None), Product.min_stock > 0)
        self.reorder_point = case(
            (and_(level_set, or_(~min_set, Product.reorder_level > Product.min_stock)), Product.reorder_level),
            (min_set, Product.min_stock), else_=None)

    def join(self, q):
        return (q.outerjoin(self.totals, self.totals.c.product_id == Product.id)
                .outerjoin(self.res, self.res.c.product_id == Product.id))


#: Ready-made views in the Saved searches menu (spec section 20). A dict is a search; "page:<key>"
#: opens that screen instead (its default filter already shows the open items).
BUILTIN_VIEWS: tuple[tuple[str, dict | str], ...] = (
    ("Reorder Required", {"stock_status": "LOW_STOCK"}),
    ("Products Without Bin", {"default_bin": "WITHOUT"}),
    ("Missing Product Photos", {"photo": "WITHOUT"}),
    ("Recently Received", {"quick": "RECENTLY_RECEIVED"}),
    ("Recently Issued", {"quick": "RECENTLY_ISSUED"}),
    ("Delivery Problems (open)", "page:discrepancies"),
    ("Outstanding Purchase Orders", "page:purchase_orders"),
    ("Active Reservations", "page:reservations"),
)

#: Movements that send stock to a project or bring it back (net quantity per project).
SITE_MOVEMENT_TYPES = (MovementType.GOODS_OUT.value, MovementType.RETURN_FROM_SITE.value)

#: Order and heading of the groups in "Other matches" (spec: products, locations, project history...).
GROUP_TITLES = {
    "Location": "LOCATIONS", "Project History": "PROJECT HISTORY", "Movements": "MOVEMENTS",
    "Delivery Note": "DELIVERY NOTES", "Purchase Order": "PURCHASE ORDERS", "Delivery Problem": "DELIVERY PROBLEMS",
    "Reservation": "RESERVATIONS", "Goods Out": "GOODS OUT", "Return": "RETURNS",
    "Bin": "BINS", "Historical Bin": "BINS", "Project": "PROJECTS", "Historical Project": "PROJECTS",
    "Supplier": "SUPPLIERS", "Historical Supplier": "SUPPLIERS",
}


# --- the service -------------------------------------------------------------------

class SearchService(BaseService):
    DEFAULT_LIMIT = 500

    # ---- products ----------------------------------------------------------
    def products(self, c: SearchCriteria, limit: int = DEFAULT_LIMIT, sort: str = "relevance",
                 descending: bool = False) -> SearchResult:
        self.require(P.PRODUCTS_VIEW)
        c.validate()
        st = StockColumns()
        conds = self._product_conditions(c, st, products_mode=True)
        order = self._product_order(sort, descending, c.text, st)
        with self.db.read_session() as s:
            def filtered(q):
                return self._apply_related_product_filters(s, c, st.join(q).where(*conds))

            q = filtered(select(Product, st.on_hand, st.usable, st.reserved)).order_by(
                *order, Product.internal_code).limit(limit)
            total, rows = 0, []
            if c.text.strip():
                # Text is tested on every product anyway: count in the same pass (count() OVER ()).
                for p, oh, us, rs, n in s.execute(q.add_columns(func.count().over())).unique():
                    rows.append(ProductHit(p, Decimal(oh or 0), Decimal(us or 0), Decimal(rs or 0)))
                    total = n
            else:
                total = s.scalar(select(func.count()).select_from(filtered(select(Product.id)).subquery()))
                if total:
                    rows = [ProductHit(p, Decimal(oh or 0), Decimal(us or 0), Decimal(rs or 0))
                            for p, oh, us, rs in s.execute(q).unique()]
            if rows:
                by_id = {r.product.id: r for r in rows}
                levels = s.execute(
                    select(StockLevel.product_id, Bin, StockLevel.quantity).join(Bin, Bin.id == StockLevel.bin_id)
                    .where(StockLevel.product_id.in_(by_id), StockLevel.quantity != ZERO)
                    .order_by(StockLevel.quantity.desc(), Bin.full_code))
                for pid, b, qty in levels.unique():
                    by_id[pid].bins.append(BinQty(b, qty))
            exact = self._exact_product(s, c.text)
        return SearchResult(rows, total or 0, limit, exact)

    def _product_order(self, sort: str, descending: bool, text: str, st: StockColumns):
        status = case((st.reorder_point.is_(None), 3), (st.available <= 0, 0), (st.available <= st.reorder_point, 1),
                      else_=2)
        keys = {
            "code": Product.internal_code,
            "name": Product.name,
            "description": Product.description,
            "qty": st.on_hand,
            "available": st.available,
            "unit": select(Unit.code).where(Unit.id == Product.unit_id).scalar_subquery(),
            "supplier": select(Supplier.name).where(Supplier.id == Product.supplier_id).scalar_subquery(),
            "bin": select(func.min(Bin.code)).join(StockLevel, StockLevel.bin_id == Bin.id).where(
                StockLevel.product_id == Product.id, StockLevel.quantity != ZERO).scalar_subquery(),
            "location": select(func.min(Bin.full_code)).join(StockLevel, StockLevel.bin_id == Bin.id).where(
                StockLevel.product_id == Product.id, StockLevel.quantity != ZERO).scalar_subquery(),
            "status": status,
            "brand": Product.brand,
            "category": select(Category.name).where(Category.id == Product.category_id).scalar_subquery(),
        }
        if sort in keys:
            col = keys[sort]
            return [col.desc() if descending else col.asc()]
        # Relevance: exact code / barcode first, then codes starting with the text, then the rest.
        key = code_key(text)
        if not key:
            return []
        return [case((code_key_sql(Product.internal_code) == key, 0), (code_key_sql(Product.barcode) == key, 0),
                     (code_key_sql(Product.supplier_code) == key, 1),
                     (code_key_sql(Product.internal_code).like(key_pattern(key)[1:], escape="\\"), 2), else_=3)]

    def _product_conditions(self, c: SearchCriteria, st: StockColumns, products_mode: bool) -> list:
        """Conditions on ``Product`` (and its stock totals) shared by both result lists."""
        conds: list = []
        if c.text.strip() and products_mode:
            conds.append(or_(text_filter(c.text), self._product_doc_match(c.text)))
        archive = c.archive or ("ACTIVE" if products_mode else "ALL")
        if archive == "ACTIVE":
            conds.append(Product.is_active.is_(True))
        elif archive == "ARCHIVED":
            conds.append(Product.is_active.is_(False))
        for attr, col in (("product_code", Product.internal_code), ("supplier_code", Product.supplier_code),
                          ("barcode", Product.barcode)):
            val = (getattr(c, attr) or "").strip()
            if val:
                key = code_key(val)
                conds.append(or_(_ilike(col, val), code_key_sql(col).like(key_pattern(key), escape="\\"))
                             if key else _ilike(col, val))
        for attr, col in (("name", Product.name), ("description", Product.description), ("brand", Product.brand)):
            val = (getattr(c, attr) or "").strip()
            if val:
                conds.append(_ilike(col, val))
        if c.photo in PHOTO_CHOICES:
            has = exists().where(Attachment.entity_type == EntityType.PRODUCT.value, Attachment.entity_id == Product.id,
                                 Attachment.kind == AttachmentKind.PHOTO.value, Attachment.is_deleted.is_(False))
            conds.append(has if c.photo == "WITH" else ~has)
        if c.default_bin == "WITH":
            conds.append(Product.default_bin_id.is_not(None))
        elif c.default_bin == "WITHOUT":
            conds.append(Product.default_bin_id.is_(None))
        if c.category_id:
            conds.append(Product.category_id.in_(
                select(Category.id).where((Category.id == c.category_id) | (Category.parent_id == c.category_id))))
        # Stock. Low = reorder required: Available (usable - reserved) at or below the reorder level /
        # minimum, exactly as the Reorder Required list judges it.
        on_hand = st.on_hand
        low = and_(st.reorder_point.is_not(None), st.available <= st.reorder_point)
        status_cond = {"IN_STOCK": on_hand > 0, "OUT_OF_STOCK": on_hand <= 0, "LOW_STOCK": low}
        if c.stock_status in status_cond:
            conds.append(status_cond[c.stock_status])
        if c.quick in status_cond:
            conds.append(status_cond[c.quick])
        if c.quick == "FINISHED":
            conds.append(Product.item_type == ItemType.FINISHED.value)
        elif c.quick == "RAW":
            conds.append(Product.item_type == ItemType.RAW_MATERIAL.value)
        if c.qty_gt is not None:
            conds.append(on_hand > c.qty_gt)
        if c.qty_lt is not None:
            conds.append(on_hand < c.qty_lt)
        return conds

    def _product_doc_match(self, text: str):
        """Products that arrived / left on a document whose number or reference matches the search
        (a delivery note number typed into the search box finds the products on it)."""
        like = like_pattern(text.strip())
        cond = or_(_ilike(StockMovement.doc_number, text.strip()), StockMovement.reference.ilike(like, escape="\\"))
        dn_key = normalise_dn_number(text)
        sub = select(StockMovement.product_id).where(cond)
        if len(dn_key) >= 3:
            dn_ids = select(DeliveryNote.id).where(DeliveryNote.dn_number_norm.contains(dn_key, autoescape=True))
            return or_(Product.id.in_(sub), Product.id.in_(
                select(DeliveryNoteLine.product_id).where(DeliveryNoteLine.delivery_note_id.in_(dn_ids))))
        return Product.id.in_(sub)

    # ---- shared filter pieces -------------------------------------------------
    def _location_path(self, s, c: SearchCriteria) -> str | None:
        """Path of the most specific location chosen (shelf beats rack beats zone...)."""
        for attr, _kind in reversed(LOCATION_LEVELS):
            loc_id = getattr(c, attr)
            if loc_id:
                loc = s.get(Location, loc_id)
                if loc is None:
                    raise NotFoundError("The chosen location no longer exists.")
                return loc.path
        return None

    def _bin_ids(self, s, c: SearchCriteria):
        """Subquery of the bins the location / bin filters allow, or None for "any bin"."""
        path = self._location_path(s, c)
        if path is None and not c.bin_id:
            return None
        q = select(Bin.id)
        if path is not None:
            q = q.where(Bin.full_code.like(prefix_pattern(path), escape="\\"))
        if c.bin_id:
            q = q.where(Bin.id == c.bin_id)
        return q

    def _site_ids(self, c: SearchCriteria):
        conds = []
        if c.site_id:
            conds.append(Site.id == c.site_id)
        for attr, col in (("project_number", Site.project_number), ("customer", Site.customer),
                          ("project_manager", Site.project_manager)):
            val = (getattr(c, attr) or "").strip()
            if val:
                conds.append(_ilike(col, val))
        if c.project_status == "ACTIVE":
            conds.append(Site.status.not_in(CLOSED_SITE_STATUSES))
        elif c.project_status == "CLOSED":
            conds.append(Site.status.in_(CLOSED_SITE_STATUSES))
        return select(Site.id).where(*conds) if conds else None

    def _movement_conditions(self, c: SearchCriteria, m=StockMovement, products_mode: bool = False) -> list:
        """Conditions on one ledger row. In product results they are applied together to the same
        movement ("sent to Surrey in the last 30 days" means one movement that did both)."""
        conds: list = []
        types = [t for g in c.movement_types for t in MOVEMENT_GROUPS.get(g, ("", ()))[1]]
        if "REVERSAL" in c.movement_types:
            conds.append(or_(m.movement_type.in_(types), m.reversal_of_id.is_not(None)) if types
                         else m.reversal_of_id.is_not(None))
        elif types:
            conds.append(m.movement_type.in_(types))
        d_from, d_to = c.date_range()
        if d_from:
            conds.append(m.occurred_at >= _start(d_from))
        if d_to:
            conds.append(m.occurred_at <= _end(d_to))
        sites = self._site_ids(c)
        if sites is not None:
            conds.append(m.site_id.in_(sites))
        if (c.dn_number or "").strip():
            key = normalise_dn_number(c.dn_number)
            dn = select(DeliveryNote.id).where(or_(_ilike(DeliveryNote.dn_number, c.dn_number.strip()),
                                                   *([DeliveryNote.dn_number_norm.contains(key, autoescape=True)]
                                                     if key else [])))
            conds.append(and_(m.doc_type == EntityType.DELIVERY_NOTE.value, m.doc_id.in_(dn)))
        if (c.po_number or "").strip() and not products_mode:
            conds.append(and_(m.doc_type == EntityType.DELIVERY_NOTE.value, m.doc_id.in_(self._po_dn_ids(c.po_number))))
        if (c.reference or "").strip():
            ref = c.reference.strip()
            gi = select(GoodsIssue.id).where(_ilike(GoodsIssue.reference, ref))
            conds.append(or_(_ilike(m.reference, ref), _ilike(m.doc_number, ref),
                             and_(m.doc_type == EntityType.GOODS_ISSUE.value, m.doc_id.in_(gi))))
        if c.quick in ("RECENTLY_RECEIVED", "RECENTLY_ISSUED"):
            mt = MovementType.GOODS_IN if c.quick == "RECENTLY_RECEIVED" else MovementType.GOODS_OUT
            conds += [m.movement_type == mt.value, m.reversal_of_id.is_(None),
                      m.occurred_at >= _start(date.today() - timedelta(days=RECENT_DAYS - 1))]
        if not products_mode and c.attachment in ATTACHMENT_CHOICES:
            has = exists().where(Attachment.entity_type == m.doc_type, Attachment.entity_id == m.doc_id,
                                 Attachment.is_deleted.is_(False))
            conds.append(has if c.attachment == "WITH" else ~has)
        return conds

    # ---- products, continued: filters that live on other tables ----------------
    @staticmethod
    def _po_ids(po_number: str):
        """Purchase orders whose number or supplier reference contains the text."""
        t = po_number.strip()
        return select(PurchaseOrder.id).where(or_(_ilike(PurchaseOrder.po_number, t),
                                                  _ilike(PurchaseOrder.supplier_reference, t)))

    def _po_dn_ids(self, po_number: str):
        """Delivery notes received against those orders, or quoting the number as their PO reference."""
        return select(DeliveryNote.id).where(or_(DeliveryNote.purchase_order_id.in_(self._po_ids(po_number)),
                                                 _ilike(DeliveryNote.po_reference, po_number.strip())))

    def _apply_related_product_filters(self, s, c: SearchCriteria, q):
        if (c.po_number or "").strip():
            # Products on the order: ordered on it or delivered against it.
            ordered = select(PurchaseOrderLine.product_id).where(
                PurchaseOrderLine.purchase_order_id.in_(self._po_ids(c.po_number)))
            delivered = select(DeliveryNoteLine.product_id).where(
                DeliveryNoteLine.delivery_note_id.in_(self._po_dn_ids(c.po_number)))
            q = q.where(or_(Product.id.in_(ordered), Product.id.in_(delivered)))
        if c.supplier_id:
            received = select(StockMovement.product_id).where(
                StockMovement.supplier_id == c.supplier_id,
                StockMovement.movement_type == MovementType.GOODS_IN.value)
            q = q.where(or_(Product.supplier_id == c.supplier_id, Product.id.in_(received)))
        bins = self._bin_ids(s, c)
        if bins is not None:
            q = q.where(Product.id.in_(select(StockLevel.product_id).where(
                StockLevel.bin_id.in_(bins), StockLevel.quantity != ZERO)))
        mconds = self._movement_conditions(c, products_mode=True)
        if mconds:
            q = q.where(Product.id.in_(select(StockMovement.product_id).where(*mconds)))
        if c.attachment in ATTACHMENT_CHOICES:
            has = exists().where(Attachment.entity_type == EntityType.PRODUCT.value,
                                 Attachment.entity_id == Product.id, Attachment.is_deleted.is_(False))
            q = q.where(has if c.attachment == "WITH" else ~has)
        return q

    # ---- transactions (the movement ledger) -------------------------------------
    def movements(self, c: SearchCriteria, limit: int = DEFAULT_LIMIT, sort: str = "date",
                  descending: bool = True) -> SearchResult:
        self.require(P.STOCK_VIEW)
        c.validate()
        m = StockMovement
        with self.db.read_session() as s:
            conds = self._movement_conditions(c)
            if c.supplier_id:
                conds.append(or_(m.supplier_id == c.supplier_id, m.product_id.in_(
                    select(Product.id).where(Product.supplier_id == c.supplier_id))))
            bins = self._bin_ids(s, c)
            if bins is not None:
                conds.append(or_(m.from_bin_id.in_(bins), m.to_bin_id.in_(bins)))
            if c.text.strip():
                conds.append(self._movement_text(c.text))
            st = StockColumns()
            pconds = self._product_conditions(c, st, products_mode=False)
            if pconds:
                prod = select(Product.id)
                if self._needs_stock_totals(c):
                    prod = st.join(prod)
                conds.append(m.product_id.in_(prod.where(*pconds)))
            order = self._movement_order(sort, descending)
            opts = (selectinload(m.product), selectinload(m.from_bin), selectinload(m.to_bin),
                    selectinload(m.site), selectinload(m.supplier), selectinload(m.user))
            if c.text.strip():
                # Text has to be checked on every row anyway: count in the same pass (count() OVER ()).
                total, rows = 0, []
                for mv, n in s.execute(select(m, func.count().over()).where(*conds).order_by(*order)
                                       .limit(limit).options(*opts)):
                    rows.append(mv)
                    total = n
            else:
                # Filters only: a separate COUNT, so the list itself is read straight off the indexes
                # (newest first) and stops after `limit` rows - a window count would prevent that.
                total = s.scalar(select(func.count(m.id)).where(*conds))
                rows = list(s.scalars(select(m).where(*conds).order_by(*order).limit(limit).options(*opts))) \
                    if total else []
            exact = self._exact_product(s, c.text)
        return SearchResult(rows, total or 0, limit, exact)

    @staticmethod
    def _needs_stock_totals(c: SearchCriteria) -> bool:
        return bool(c.stock_status or c.qty_gt is not None or c.qty_lt is not None or c.quick in STOCK_STATUSES)

    def _movement_text(self, text: str):
        m = StockMovement
        parts = []
        for term in search_terms(text):
            like = like_pattern(term)
            parts.append(or_(
                m.doc_number.ilike(like, escape="\\"), m.reference.ilike(like, escape="\\"),
                m.notes.ilike(like, escape="\\"),
                # historical names: finds 'B04' or an old project name after a rename / removal
                *(col.ilike(like, escape="\\") for col in SNAPSHOT_TEXT_COLUMNS),
                m.product_id.in_(select(Product.id).where(or_(
                    Product.internal_code.ilike(like, escape="\\"), Product.name.ilike(like, escape="\\"),
                    Product.supplier_code.ilike(like, escape="\\"), Product.barcode.ilike(like, escape="\\"),
                    Product.brand.ilike(like, escape="\\")))),
                m.site_id.in_(select(Site.id).where(or_(Site.name.ilike(like, escape="\\"),
                                                        Site.project_number.ilike(like, escape="\\"),
                                                        Site.customer.ilike(like, escape="\\")))),
                m.supplier_id.in_(select(Supplier.id).where(or_(Supplier.name.ilike(like, escape="\\"),
                                                                Supplier.code.ilike(like, escape="\\")))),
                m.from_bin_id.in_(select(Bin.id).where(Bin.full_code.ilike(like, escape="\\"))),
                m.to_bin_id.in_(select(Bin.id).where(Bin.full_code.ilike(like, escape="\\"))),
                m.user_id.in_(select(User.id).where(or_(User.username.ilike(like, escape="\\"),
                                                        User.full_name.ilike(like, escape="\\")))),
            ))
        words = and_(*parts)
        key = code_key(text)
        if len(key) < 2:
            return words
        pat = key_pattern(key)
        bins = select(Bin.id).where(code_key_sql(Bin.code).like(pat, escape="\\"))
        dn_key = normalise_dn_number(text)
        return or_(
            words,
            m.product_id.in_(select(Product.id).where(or_(
                code_key_sql(Product.internal_code).like(pat, escape="\\"),
                code_key_sql(Product.supplier_code).like(pat, escape="\\"),
                code_key_sql(Product.barcode).like(pat, escape="\\")))),
            m.from_bin_id.in_(bins), m.to_bin_id.in_(bins),
            code_key_sql(m.product_code_snapshot).like(pat, escape="\\"),
            *([and_(m.doc_type == EntityType.DELIVERY_NOTE.value, m.doc_id.in_(
                select(DeliveryNote.id).where(DeliveryNote.dn_number_norm.contains(dn_key, autoescape=True))))]
              if len(dn_key) >= 3 else []),
        )

    def _movement_order(self, sort: str, descending: bool):
        m = StockMovement
        keys = {
            "date": m.occurred_at,
            "type": m.movement_type,
            "qty": m.quantity,
            # Sorted by the names shown, i.e. as they were at the time
            "code": m.product_code_snapshot,
            "product": m.product_name_snapshot,
            "supplier": m.supplier_name_snapshot,
            "project": m.site_name_snapshot,
            "bin": func.coalesce(m.from_bin_name_snapshot, m.to_bin_name_snapshot),
            "reference": func.coalesce(m.doc_number, m.reference),
            "user": m.user_name_snapshot,
        }
        col = keys.get(sort, m.occurred_at)
        direction = col.desc() if descending else col.asc()
        tie = m.id.desc() if descending else m.id.asc()
        return [direction, tie]

    # ---- exact identification (barcode scanner, typed code) ----------------------
    def _exact_product(self, s, text: str) -> Product | None:
        key = code_key(text)
        if len(key) < 2:
            return None
        typed = " ".join(text.split())
        active = Product.is_active.is_(True)
        # Fast path (scanner): codes are stored upper case and barcodes as scanned - both indexed.
        for cond in (Product.internal_code == typed.upper(), Product.barcode == typed,
                     Product.barcode == typed.upper()):
            p = s.scalar(select(Product).where(active, cond).limit(1))
            if p is not None:
                return p
        # Typed differently ("h 3311", "h-3311"): compare without spaces / punctuation.
        for col in (Product.internal_code, Product.barcode, Product.supplier_code):
            found = list(s.scalars(select(Product).where(active, code_key_sql(col) == key).limit(2)).unique())
            if len(found) == 1:
                return found[0]
            if len(found) > 1:
                return None  # ambiguous (same supplier code from two suppliers): let the user choose
        return None

    def exact_product(self, text: str) -> Product | None:
        """The one active product a scanned barcode / typed code identifies, or None."""
        with self.db.read_session() as s:
            return self._exact_product(s, text)

    # ---- other records matching the text (bins, projects, suppliers, documents) ----
    def matches(self, text: str, per_kind: int = 8) -> list[SearchHit]:
        """Records other than products that the text identifies: open them for their own view."""
        text = (text or "").strip()
        if len(text) < 2:
            return []
        like = like_pattern(text)
        key = code_key(text)
        dn_key = normalise_dn_number(text)
        ctx = self.ctx
        hits: list[SearchHit] = []
        with self.db.read_session() as s:
            if ctx.has(P.LOCATIONS_VIEW) or ctx.has(P.PRODUCTS_VIEW):
                exact_bin = or_(func.upper(Bin.code) == text.upper(), code_key_sql(Bin.code) == key)
                cond = or_(Bin.full_code.ilike(like, escape="\\"),
                           *([code_key_sql(Bin.code).like(key_pattern(key), escape="\\")] if len(key) >= 2 else []))
                # A bin renamed or removed since is still found by the code it had on old transactions.
                m = StockMovement
                by_history = select(m.from_bin_id).where(m.from_bin_name_snapshot.ilike(like, escape="\\")).union(
                    select(m.to_bin_id).where(m.to_bin_name_snapshot.ilike(like, escape="\\")))
                cond = or_(cond, Bin.id.in_(by_history))
                for b in s.scalars(select(Bin).where(cond).order_by(case((exact_bin, 0), else_=1),
                                                                    Bin.is_active.desc(), Bin.full_code)
                                   .limit(per_kind)):
                    count = s.scalar(select(func.count(StockLevel.id)).where(StockLevel.bin_id == b.id,
                                                                            StockLevel.quantity != ZERO))
                    if b.is_active:
                        hits.append(SearchHit("Bin", EntityType.BIN.value, b.id, b.code,
                                              " | ".join(x for x in (location_of(b), f"{count} product(s)",
                                                                     None if b.is_available else "Quarantine") if x)))
                    else:
                        moves = s.scalar(select(func.count(m.id)).where(or_(m.from_bin_id == b.id,
                                                                            m.to_bin_id == b.id)))
                        hits.append(SearchHit("Historical Bin", EntityType.BIN.value, b.id, b.code,
                                              f"{location_of(b)} | Archived - removed from active use | "
                                              f"{moves:,} historical movement(s)"))
            if ctx.has(P.SITES_VIEW):
                renamed = select(StockMovement.site_id).where(or_(
                    StockMovement.site_name_snapshot.ilike(like, escape="\\"),
                    StockMovement.site_number_snapshot.ilike(like, escape="\\")))
                for site in s.scalars(select(Site).where(or_(
                        Site.project_number.ilike(like, escape="\\"), Site.name.ilike(like, escape="\\"),
                        Site.customer.ilike(like, escape="\\"), Site.postcode.ilike(like, escape="\\"),
                        Site.project_manager.ilike(like, escape="\\"), Site.id.in_(renamed)))
                        .order_by(Site.name).limit(per_kind)):
                    kind = "Historical Project" if site.status in CLOSED_SITE_STATUSES else "Project"
                    hits.append(SearchHit(kind, EntityType.SITE.value, site.id, site.display,
                                          " | ".join(x for x in (site.customer, SiteStatus(site.status).label,
                                                                 site.project_manager) if x)))
            supplied_as = select(StockMovement.supplier_id).where(
                StockMovement.supplier_name_snapshot.ilike(like, escape="\\"))
            for sup in s.scalars(select(Supplier).where(or_(Supplier.name.ilike(like, escape="\\"),
                                                            Supplier.code.ilike(like, escape="\\"),
                                                            Supplier.id.in_(supplied_as)))
                                 .order_by(Supplier.name).limit(per_kind)):
                hits.append(SearchHit("Supplier" if sup.is_active else "Historical Supplier",
                                      EntityType.SUPPLIER.value, sup.id, sup.name,
                                      " | ".join(x for x in (sup.code, sup.contact_name,
                                                             None if sup.is_active else "Archived") if x)))
            if ctx.has(P.GOODS_IN_VIEW):
                for dn in s.scalars(select(DeliveryNote).where(or_(
                        DeliveryNote.dn_number.ilike(like, escape="\\"),
                        DeliveryNote.po_reference.ilike(like, escape="\\"),
                        DeliveryNote.supplier_id.in_(select(Supplier.id).where(Supplier.name.ilike(like, escape="\\"))),
                        *([DeliveryNote.dn_number_norm.contains(dn_key, autoescape=True)] if dn_key else [])))
                        .order_by(DeliveryNote.delivery_date.desc()).limit(per_kind)):
                    hits.append(SearchHit("Delivery Note", EntityType.DELIVERY_NOTE.value, dn.id,
                                          f"{dn.dn_number} - {dn.supplier_label}",
                                          f"{fmt_date(dn.delivery_date)} | {dn.status.title()}"
                                          + (f" | PO {dn.po_reference}" if dn.po_reference else "")))
            if ctx.has(P.GOODS_OUT_VIEW):
                for gi in s.scalars(select(GoodsIssue).where(or_(
                        GoodsIssue.issue_number.ilike(like, escape="\\"), GoodsIssue.reference.ilike(like, escape="\\"),
                        GoodsIssue.requested_by.ilike(like, escape="\\")))
                        .order_by(GoodsIssue.issue_date.desc()).limit(per_kind)):
                    hits.append(SearchHit("Goods Out", EntityType.GOODS_ISSUE.value, gi.id,
                                          f"{gi.issue_number} - {gi.site_label}",
                                          f"{fmt_date(gi.issue_date)} | Ref {gi.reference or '-'} | "
                                          f"{gi.status.title()}"))
                for r in s.scalars(select(StockReturn).where(or_(StockReturn.return_number.ilike(like, escape="\\"),
                                                                 StockReturn.reference.ilike(like, escape="\\")))
                                   .limit(per_kind)):
                    hits.append(SearchHit("Return", EntityType.RETURN.value, r.id, r.return_number,
                                          f"{fmt_date(r.return_date)} | {r.reference or ''}"))
            if ctx.has(P.PURCHASING_VIEW):
                for po in s.scalars(select(PurchaseOrder).where(or_(
                        PurchaseOrder.po_number.ilike(like, escape="\\"),
                        PurchaseOrder.supplier_reference.ilike(like, escape="\\")))
                        .order_by(PurchaseOrder.order_date.desc(), PurchaseOrder.id.desc()).limit(per_kind)):
                    hits.append(SearchHit("Purchase Order", EntityType.PURCHASE_ORDER.value, po.id,
                                          f"{po.po_number} - {po.supplier_label}",
                                          " | ".join(x for x in (fmt_date(po.order_date), POStatus(po.status).label,
                                                                 po.supplier_reference) if x)))
                for d in s.scalars(select(DeliveryDiscrepancy).where(DeliveryDiscrepancy.number.ilike(
                        like, escape="\\")).order_by(DeliveryDiscrepancy.id.desc()).limit(per_kind)):
                    hits.append(SearchHit("Delivery Problem", EntityType.DISCREPANCY.value, d.id, d.number,
                                          f"{d.product.internal_code} | {d.status.title()}"))
            if ctx.has(P.STOCK_VIEW):
                for r in s.scalars(select(StockReservation).where(StockReservation.number.ilike(
                        like, escape="\\")).order_by(StockReservation.id.desc()).limit(per_kind)):
                    hits.append(SearchHit("Reservation", EntityType.RESERVATION.value, r.id, r.number,
                                          f"{r.product.internal_code} {fmt_qty(r.quantity)} for {r.site_label} | "
                                          f"{ReservationStatus(r.status).label}"))
        return hits

    # ---- one product, grouped (what a scan of H3311 should answer at a glance) ----
    def product_groups(self, product_id: int, per_kind: int = 8) -> list[SearchHit]:
        """Where it is, which projects had it, which delivery notes brought it, how many movements."""
        hits: list[SearchHit] = []
        m = StockMovement
        with self.db.read_session() as s:
            p = s.get(Product, product_id)
            if p is None:
                return []
            unit = p.unit.code
            for lvl in s.scalars(select(StockLevel).where(StockLevel.product_id == product_id,
                                                          StockLevel.quantity != ZERO)
                                 .order_by(StockLevel.quantity.desc())).unique():
                hits.append(SearchHit("Location", EntityType.BIN.value, lvl.bin_id, lvl.bin.code,
                                      f"{fmt_qty(lvl.quantity)} {unit} | {location_of(lvl.bin)}"
                                      + ("" if lvl.bin.is_available else " | Quarantine")))
            if self.ctx.has(P.SITES_VIEW):
                net: dict[int, Decimal] = {}
                for site_id, from_bin, qty in s.execute(
                        select(m.site_id, m.from_bin_id, m.quantity).where(
                            m.product_id == product_id, m.site_id.is_not(None),
                            m.movement_type.in_(SITE_MOVEMENT_TYPES))):
                    net[site_id] = net.get(site_id, ZERO) + (qty if from_bin else -qty)
                for site_id, qty in sorted(net.items(), key=lambda kv: -kv[1])[:per_kind]:
                    site = s.get(Site, site_id)
                    hits.append(SearchHit("Project History", EntityType.SITE.value, site_id, site.display,
                                          f"{fmt_qty(qty)} {unit} net (issued less returned) | "
                                          f"{SiteStatus(site.status).label}"))
            if self.ctx.has(P.GOODS_IN_VIEW):
                dns = select(DeliveryNoteLine.delivery_note_id).where(DeliveryNoteLine.product_id == product_id)
                for dn in s.scalars(select(DeliveryNote).where(DeliveryNote.id.in_(dns))
                                    .order_by(DeliveryNote.delivery_date.desc(), DeliveryNote.id.desc())
                                    .limit(per_kind)):
                    hits.append(SearchHit("Delivery Note", EntityType.DELIVERY_NOTE.value, dn.id,
                                          f"{dn.dn_number} - {dn.supplier_label}",
                                          f"{fmt_date(dn.delivery_date)} | {dn.status.title()}"))
            if self.ctx.has(P.STOCK_VIEW):
                count = s.scalar(select(func.count(m.id)).where(m.product_id == product_id)) or 0
                hits.append(SearchHit("Movements", "MOVEMENTS", product_id, f"{count:,} matching transaction(s)",
                                      "Open the full movement history"))
        return hits

    def search(self, text: str, per_kind: int = 50) -> list[SearchHit]:
        """Flat list of every kind of record matching the text (products first)."""
        text = (text or "").strip()
        if len(text) < 2:
            return []
        hits: list[SearchHit] = []
        with self.db.read_session() as s:
            for p in s.scalars(select(Product).where(text_filter(text)).order_by(Product.internal_code)
                               .limit(per_kind)).unique():
                where = p.default_bin.full_code if p.default_bin else ""
                hits.append(SearchHit("Product", EntityType.PRODUCT.value, p.id, f"{p.internal_code} - {p.name}",
                                      " | ".join(x for x in (p.supplier_code, p.supplier.name if p.supplier else None,
                                                             where, "inactive" if not p.is_active else None) if x)))
        hits += self.matches(text, per_kind)
        like = like_pattern(text)
        seen_docs = {(h.entity_type, h.entity_id) for h in hits}
        with self.db.read_session() as s:
            # Reference numbers that only exist on movements (job numbers, adjustment/transfer numbers)
            for m in s.scalars(select(StockMovement).where(or_(StockMovement.reference.ilike(like, escape="\\"),
                                                               StockMovement.doc_number.ilike(like, escape="\\")))
                               .order_by(StockMovement.occurred_at.desc()).limit(per_kind)):
                if (m.doc_type, m.doc_id) in seen_docs:
                    continue
                hits.append(SearchHit("Movement", "MOVEMENT", m.id,
                                      f"{m.doc_number or m.reference} - {m.product_code}",
                                      f"{MovementType(m.movement_type).label} | {fmt_qty(m.quantity)} "
                                      f"{m.product.unit.code} | {fmt_date(m.occurred_at)}"))
        return hits

    # ---- chips ----------------------------------------------------------------
    def describe(self, c: SearchCriteria) -> list[tuple[str, str]]:
        """(filter key, 'Supplier: Egger') for every active filter, for the chips above the results."""
        out = []
        with self.db.read_session() as s:
            def name_of(model, oid, attr="name"):
                obj = s.get(model, oid)
                return getattr(obj, attr) if obj is not None else f"#{oid} (deleted)"

            for key in c.active_keys():
                v = getattr(c, key, None)
                if key == "category_id":
                    obj = s.get(Category, v)
                    val = obj.full_name if obj else f"#{v}"
                elif key in ("warehouse_id", "zone_id", "rack_id", "shelf_id"):
                    val = name_of(Location, v, "path")
                elif key == "bin_id":
                    val = name_of(Bin, v, "full_code")
                elif key == "supplier_id":
                    val = name_of(Supplier, v)
                elif key == "site_id":
                    obj = s.get(Site, v)
                    val = obj.name if obj else f"#{v}"
                elif key == "archive":
                    val = ARCHIVE_CHOICES.get(v, v)
                elif key == "stock_status":
                    val = STOCK_STATUSES.get(v, v)
                elif key == "project_status":
                    val = PROJECT_STATUS_CHOICES.get(v, v)
                elif key == "attachment":
                    out.append((key, ATTACHMENT_CHOICES.get(v, v)))
                    continue
                elif key == "photo":
                    out.append((key, PHOTO_CHOICES.get(v, v)))
                    continue
                elif key == "default_bin":
                    out.append((key, DEFAULT_BIN_CHOICES.get(v, v)))
                    continue
                elif key == "movement_types":
                    val = ", ".join(MOVEMENT_GROUPS[g][0] for g in v if g in MOVEMENT_GROUPS)
                elif key in ("qty_gt", "qty_lt"):
                    val = fmt_qty(v)
                elif key == "date":
                    if c.date_preset and c.date_preset != "CUSTOM":
                        val = DATE_PRESETS.get(c.date_preset, c.date_preset)
                    else:
                        f, t = c.date_range()
                        val = (f"{fmt_date(f)} - {fmt_date(t)}" if f and t else
                               f"from {fmt_date(f)}" if f else f"until {fmt_date(t)}")
                else:
                    val = str(v)
                out.append((key, f"{FILTER_LABELS[key]}: {val}"))
        return out

    # ---- saved and recent searches (per user) --------------------------------------
    def saved_searches(self) -> list[SavedSearch]:
        with self.db.read_session() as s:
            return list(s.scalars(select(SavedSearch).where(SavedSearch.user_id == self.user.id)
                                  .order_by(func.lower(SavedSearch.name))))

    def save_search(self, name: str, c: SearchCriteria, replace: bool = False) -> SavedSearch:
        name = clean_str(name, "name", required=True, max_len=80)
        c.validate()
        with self.db.session() as s:
            existing = s.scalar(select(SavedSearch).where(SavedSearch.user_id == self.user.id,
                                                          func.lower(SavedSearch.name) == name.lower()))
            if existing is not None:
                if not replace:
                    raise ValidationError(f"You already have a saved search called '{existing.name}'.", "name")
                existing.criteria = c.to_json()
                existing.name = name
                saved = existing
            else:
                count = s.scalar(select(func.count(SavedSearch.id)).where(SavedSearch.user_id == self.user.id))
                if count >= MAX_SAVED:
                    raise ValidationError(f"You can keep up to {MAX_SAVED} saved searches. Delete one first.")
                saved = SavedSearch(user_id=self.user.id, name=name, criteria=c.to_json())
                s.add(saved)
            s.flush()
            return saved

    def delete_saved(self, saved_id: int) -> None:
        with self.db.session() as s:
            s.execute(delete(SavedSearch).where(SavedSearch.id == saved_id, SavedSearch.user_id == self.user.id))

    def recent(self) -> list[str]:
        with self.db.read_session() as s:
            return list(s.scalars(select(RecentSearch.text).where(RecentSearch.user_id == self.user.id)
                                  .order_by(RecentSearch.searched_at.desc(), RecentSearch.id.desc())
                                  .limit(MAX_RECENT)))

    def remember(self, text: str) -> None:
        """Put a search at the top of the user's recent list (kept to the last 10)."""
        text = " ".join((text or "").split())[:120]
        if len(text) < 2 or self.ctx.user is None:
            return
        uid = self.user.id
        with self.db.session() as s:
            row = s.scalar(select(RecentSearch).where(RecentSearch.user_id == uid,
                                                      func.lower(RecentSearch.text) == text.lower()))
            if row is None:
                s.add(RecentSearch(user_id=uid, text=text, searched_at=utcnow()))
            else:
                row.text, row.searched_at = text, utcnow()
            s.flush()
            keep = select(RecentSearch.id).where(RecentSearch.user_id == uid).order_by(
                RecentSearch.searched_at.desc(), RecentSearch.id.desc()).limit(MAX_RECENT)
            s.execute(delete(RecentSearch).where(RecentSearch.user_id == uid, RecentSearch.id.not_in(keep)))

    def clear_recent(self) -> None:
        with self.db.session() as s:
            s.execute(delete(RecentSearch).where(RecentSearch.user_id == self.user.id))

    # ---- dedicated views ----------------------------------------------------------
    def bin_view(self, bin_id: int, recent: int = 20, history_limit: int = 5000) -> BinView:
        """A bin's own page. Works the same for a bin removed from active use: its full movement
        history (with the names recorded at the time) stays available for investigation."""
        with self.db.read_session() as s:
            b = s.get(Bin, bin_id)
            if b is None:
                raise NotFoundError("Bin not found.")
            path = []
            loc = b.location
            while loc is not None:
                path.append((LocationKind(loc.kind).label, loc.name or loc.code))
                loc = loc.parent
            path.reverse()
            contents = list(s.scalars(select(StockLevel).join(Product, Product.id == StockLevel.product_id)
                                      .where(StockLevel.bin_id == bin_id, StockLevel.quantity != ZERO)
                                      .order_by(Product.internal_code)))
            opts = (selectinload(StockMovement.product), selectinload(StockMovement.from_bin),
                    selectinload(StockMovement.to_bin), selectinload(StockMovement.site),
                    selectinload(StockMovement.supplier), selectinload(StockMovement.user))
            order = (StockMovement.occurred_at.desc(), StockMovement.id.desc())
            recent_in = list(s.scalars(select(StockMovement).where(StockMovement.to_bin_id == bin_id)
                                       .order_by(*order).limit(recent).options(*opts)))
            recent_out = list(s.scalars(select(StockMovement).where(StockMovement.from_bin_id == bin_id)
                                        .order_by(*order).limit(recent).options(*opts)))
            either = or_(StockMovement.from_bin_id == bin_id, StockMovement.to_bin_id == bin_id)
            history = list(s.scalars(select(StockMovement).where(either).order_by(*order)
                                     .limit(history_limit).options(*opts)))
            history_count = s.scalar(select(func.count(StockMovement.id)).where(either)) or 0
            view = BinView(b, path, contents, recent_in, recent_out, history, history_count)
            if not b.is_active:
                entry = last_removal(s, EntityType.BIN.value, b.id)
                if entry is not None:
                    view.removed_on, view.removed_by = entry.occurred_at, entry.username
                    view.removed_reason = json.loads(entry.new_values or "{}").get("reason")
        return view

    def supplier_view(self, supplier_id: int) -> SupplierView:
        m = StockMovement
        with self.db.read_session() as s:
            sup = s.get(Supplier, supplier_id)
            if sup is None:
                raise NotFoundError("Supplier not found.")
            # Received = Goods In into a bin, minus reversals (a reversed Goods In leaves a bin).
            signed = case((m.to_bin_id.is_not(None), m.quantity), else_=-m.quantity)
            received = {
                pid: (Decimal(qty or 0), docs, last)
                for pid, qty, docs, last in s.execute(
                    select(m.product_id, type_coerce(func.sum(signed), Quantity()),
                           func.count(func.distinct(case((m.reversal_of_id.is_(None), m.doc_id)))),
                           type_coerce(func.max(m.occurred_at), UTCDateTime()))
                    .where(m.supplier_id == supplier_id, m.movement_type == MovementType.GOODS_IN.value)
                    .group_by(m.product_id))
            }
            ids = set(received) | set(s.scalars(select(Product.id).where(Product.supplier_id == supplier_id)))
            products = {p.id: p for p in s.scalars(select(Product).where(Product.id.in_(ids))).unique()} if ids else {}
            rows = []
            for pid, p in products.items():
                qty, docs, last = received.get(pid, (ZERO, 0, None))
                rows.append(SupplierProductRow(p, qty, docs, last))
            rows.sort(key=lambda r: r.product.internal_code)
            posted = DeliveryNote.status == "POSTED"
            count, last = s.execute(select(func.count(DeliveryNote.id), func.max(DeliveryNote.delivery_date))
                                    .where(DeliveryNote.supplier_id == supplier_id, posted)).one()
            lines = s.scalar(select(func.count(DeliveryNoteLine.id)).join(DeliveryNote).where(
                DeliveryNote.supplier_id == supplier_id, posted))
        return SupplierView(sup, rows, count or 0, last, lines or 0)
