"""Products (materials, components, consumables, finished products)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import and_, exists, func, or_, select, true
from sqlalchemy.orm import Session, aliased

from wms.core.enums import FinishedStatus, ItemType, StockStatus
from wms.core.exceptions import ConcurrencyError, DuplicateError, NotFoundError, ValidationError
from wms.core.numbers import ZERO, to_money, to_qty
from wms.core.permissions import P
from wms.db.models import (
    Bin,
    Category,
    FinishedProductInfo,
    Location,
    Product,
    StockLevel,
    StockMovement,
    Supplier,
    Unit,
)
from wms.services import audit
from wms.services.base import BaseService, clean_code, clean_str, like_pattern, prefix_pattern
from wms.services.stock_engine import product_totals_query

PRODUCT_FIELDS = (
    "item_type", "internal_code", "supplier_code", "barcode", "name", "description", "brand", "category_id", "unit_id",
    "supplier_id", "default_bin_id", "min_stock", "reorder_qty", "unit_cost", "notes", "is_active",
)
FINISHED_FIELDS = ("production_date", "site_id", "customer", "status")


@dataclass
class ProductRow:
    product: Product
    on_hand: Decimal
    available: Decimal

    @property
    def status(self) -> StockStatus:
        """OUT / LOW / OK against the minimum level, judged on stock that can be issued
        (quarantine stock does not count). Products without a minimum are never flagged."""
        p = self.product
        if p.min_stock is None or p.min_stock <= ZERO:
            return StockStatus.NO_MINIMUM
        if self.available <= ZERO:
            return StockStatus.OUT
        if self.available <= p.min_stock:
            return StockStatus.LOW
        return StockStatus.OK

    @property
    def is_low(self) -> bool:
        return self.status in (StockStatus.LOW, StockStatus.OUT)

    @property
    def stock_value(self) -> Decimal:
        return (self.on_hand * (self.product.unit_cost or ZERO)).quantize(Decimal("0.01"))

    @property
    def shortfall(self) -> Decimal:
        """Suggested order: up to min + reorder qty (or min) - on hand."""
        p = self.product
        if not self.is_low:
            return ZERO
        target = (p.min_stock or ZERO) + (p.reorder_qty or ZERO)
        return max(target - self.available, ZERO)


SEARCH_SEPARATORS = " -/._"


def code_key(text: str | None) -> str:
    """How codes are compared when the user types them differently: 'h 3311', 'H-3311' and 'H3311'
    are the same. Spaces, dashes, slashes, dots and underscores are dropped; case is ignored."""
    return "".join(ch for ch in (text or "").upper() if ch not in SEARCH_SEPARATORS)


def code_key_sql(col):
    """``code_key`` done by the database on a column, so stored codes are compared the same way."""
    expr = col
    for ch in SEARCH_SEPARATORS:
        expr = func.replace(expr, ch, "")
    return func.upper(expr)


def key_pattern(key: str) -> str:
    """'contains' LIKE pattern for a code key (wildcards typed by the user are matched literally)."""
    return "%" + key.replace("\\", "\\\\").replace("%", "\\%") + "%"


def search_terms(text: str | None, max_terms: int = 6) -> list[str]:
    """Words of a search, trimmed. 'oak 18' must match both words, in any field and any order."""
    return (text or "").split()[:max_terms]


def _stocked_in(bin_condition):
    """Ids of products currently held in a bin matching the condition. (Bins are picked first: 400
    bins are cheaper to test than 20,000 stock rows.)"""
    return select(StockLevel.product_id).where(StockLevel.quantity != ZERO,
                                               StockLevel.bin_id.in_(select(Bin.id).where(bin_condition)))


def _term_filter(term: str):
    like = like_pattern(term)
    sup = aliased(Supplier)
    cat = aliased(Category)
    stocked_in = _stocked_in(Bin.full_code.ilike(like, escape="\\"))
    return or_(
        Product.internal_code.ilike(like, escape="\\"),
        Product.supplier_code.ilike(like, escape="\\"),
        Product.barcode.ilike(like, escape="\\"),
        Product.name.ilike(like, escape="\\"),
        Product.description.ilike(like, escape="\\"),
        Product.brand.ilike(like, escape="\\"),
        exists().where(sup.id == Product.supplier_id, sup.name.ilike(like, escape="\\")),
        exists().where(cat.id == Product.category_id, cat.name.ilike(like, escape="\\")),
        Product.id.in_(stocked_in),
        exists().where(Bin.id == Product.default_bin_id, Bin.full_code.ilike(like, escape="\\")),
    )


def text_filter(text: str):
    """WHERE clause matching a product by any of its identifying fields, brand, supplier, category or bin.

    Every word must match somewhere ('oak 18'). Codes also match when typed with different spacing or
    punctuation ('h 3311' finds H3311, 'B04' finds bin B-04)."""
    terms = search_terms(text)
    if not terms:
        return true()
    words = and_(*(_term_filter(t) for t in terms))
    key = code_key(text)
    if len(key) < 2:
        return words
    pat = key_pattern(key)
    by_code = or_(code_key_sql(Product.internal_code).like(pat, escape="\\"),
                  code_key_sql(Product.supplier_code).like(pat, escape="\\"),
                  code_key_sql(Product.barcode).like(pat, escape="\\"),
                  Product.id.in_(_stocked_in(code_key_sql(Bin.code).like(pat, escape="\\"))))
    return or_(words, by_code)


class ProductService(BaseService):
    def list(
        self,
        text: str | None = None,
        item_types: list[str] | None = None,
        category_id: int | None = None,
        supplier_id: int | None = None,
        location_id: int | None = None,
        low_only: bool = False,
        include_inactive: bool = False,
        limit: int | None = None,
    ) -> list[ProductRow]:
        totals = product_totals_query().subquery()
        q = (
            select(Product, func.coalesce(totals.c.on_hand, 0), func.coalesce(totals.c.available, 0))
            .outerjoin(totals, totals.c.product_id == Product.id)
            .order_by(Product.internal_code)
        )
        if not include_inactive:
            q = q.where(Product.is_active.is_(True))
        if item_types:
            q = q.where(Product.item_type.in_(item_types))
        if category_id:
            sub = select(Category.id).where((Category.id == category_id) | (Category.parent_id == category_id))
            q = q.where(Product.category_id.in_(sub))
        if supplier_id:
            q = q.where(Product.supplier_id == supplier_id)
        if location_id:
            with self.db.read_session() as s:
                loc = s.get(Location, location_id)
                path = loc.path if loc else ""
            q = q.where(
                exists().where(
                    StockLevel.product_id == Product.id,
                    StockLevel.quantity != ZERO,
                    StockLevel.bin_id == Bin.id,
                    Bin.full_code.like(prefix_pattern(path), escape="\\"),
                )
            )
        if text and text.strip():
            q = q.where(text_filter(text))
        if limit:
            q = q.limit(limit)
        with self.db.read_session() as s:
            rows = [ProductRow(p, Decimal(oh or 0), Decimal(av or 0)) for p, oh, av in s.execute(q).unique()]
        if low_only:
            rows = [r for r in rows if r.is_low]
        return rows

    def low_stock(self) -> list[ProductRow]:
        return self.list(low_only=True)

    def get(self, product_id: int) -> Product:
        with self.db.read_session() as s:
            p = s.get(Product, product_id)
            if p is None:
                raise NotFoundError("Product not found.")
            return p

    def find_by_code(self, code: str) -> Product | None:
        """Exact match for scanner / typed input: internal code, then barcode, then supplier code.

        Internal codes and barcodes are unique, so a scan never picks the wrong product. Supplier
        codes may repeat between suppliers; an ambiguous supplier code returns None (the user
        then picks from the search list instead of getting a random match).
        """
        code = " ".join((code or "").split()).upper()
        if not code:
            return None
        with self.db.read_session() as s:
            active = Product.is_active.is_(True)
            for col in (Product.internal_code, Product.barcode):
                p = s.scalar(select(Product).where(active, func.upper(col) == code).limit(1))
                if p is not None:
                    return p
            found = list(s.scalars(select(Product).where(active, func.upper(Product.supplier_code) == code).limit(2)))
            return found[0] if len(found) == 1 else None

    def stock_by_bin(self, product_id: int) -> list[StockLevel]:
        with self.db.read_session() as s:
            return list(
                s.scalars(
                    select(StockLevel)
                    .join(Bin)
                    .where(StockLevel.product_id == product_id, StockLevel.quantity != ZERO)
                    .order_by(Bin.full_code)
                )
            )

    def movements(self, product_id: int, limit: int = 1000) -> list[StockMovement]:
        with self.db.read_session() as s:
            return list(
                s.scalars(
                    select(StockMovement)
                    .where(StockMovement.product_id == product_id)
                    .order_by(StockMovement.occurred_at.desc(), StockMovement.id.desc())
                    .limit(limit)
                )
            )

    def has_movements(self, s: Session, product_id: int) -> bool:
        return s.scalar(select(StockMovement.id).where(StockMovement.product_id == product_id).limit(1)) is not None

    # -- create / update ----------------------------------------------------
    def _validate(self, s: Session, data: dict, product_id: int | None) -> dict:
        v: dict = {}
        try:
            v["item_type"] = ItemType(data.get("item_type") or ItemType.RAW_MATERIAL).value
        except ValueError:
            raise ValidationError("Choose a valid item type.", "item_type") from None
        code = clean_code(data.get("internal_code"), "internal_code", "Internal product code", 50)
        dup = s.scalar(select(Product.id).where(func.upper(Product.internal_code) == code))
        if dup and dup != product_id:
            raise DuplicateError(f"Product code '{code}' is already used.")
        v["internal_code"] = code
        v["name"] = clean_str(data.get("name"), "name", required=True, max_len=200)
        v["supplier_code"] = clean_str(data.get("supplier_code"), "supplier_code", max_len=80)
        v["barcode"] = clean_str(data.get("barcode"), "barcode", max_len=80)
        if v["barcode"]:
            # A scanned barcode must identify exactly one product: unique among barcodes (ignoring
            # case) and never equal to another product's internal code.
            bc = v["barcode"].upper()
            dup = s.scalar(select(Product.internal_code).where(
                Product.id != (product_id or 0),
                or_(func.upper(Product.barcode) == bc, func.upper(Product.internal_code) == bc)))
            if dup:
                raise DuplicateError(f"Barcode '{v['barcode']}' is already used by product {dup}.")
        clash = s.scalar(select(Product.internal_code).where(Product.id != (product_id or 0),
                                                             func.upper(Product.barcode) == code))
        if clash:
            raise DuplicateError(f"'{code}' is the barcode of product {clash} - choose another code.")
        v["description"] = clean_str(data.get("description"))
        v["brand"] = clean_str(data.get("brand"), "brand", max_len=100)
        v["notes"] = clean_str(data.get("notes"))
        unit = s.get(Unit, data.get("unit_id")) if data.get("unit_id") else None
        if unit is None:
            raise ValidationError("Unit of measure is required.", "unit_id")
        v["unit_id"] = unit.id
        for key, model in (("category_id", Category), ("supplier_id", Supplier), ("default_bin_id", Bin)):
            val = data.get(key) or None
            if val and s.get(model, val) is None:
                raise ValidationError(f"Selected {key.replace('_id', '').replace('_', ' ')} no longer exists.", key)
            v[key] = val
        for key in ("min_stock", "reorder_qty"):
            raw = data.get(key)
            if raw in (None, ""):
                v[key] = None
            else:
                q = to_qty(raw, key)
                if q < ZERO:
                    raise ValidationError(f"{key.replace('_', ' ').capitalize()} cannot be negative.", key)
                if not unit.allow_decimal and q != q.to_integral_value():
                    raise ValidationError(f"{unit.code} quantities must be whole numbers.", key)
                v[key] = q
        raw = data.get("unit_cost")
        if raw in (None, ""):
            v["unit_cost"] = None
        else:
            c = to_money(raw)
            if c < ZERO:
                raise ValidationError("Unit cost cannot be negative.", "unit_cost")
            v["unit_cost"] = c
        v["is_active"] = bool(data.get("is_active", True))
        return v

    def save(self, product_id: int | None, data: dict, expected_version: int | None = None) -> Product:
        self.require(P.PRODUCTS_EDIT)
        with self.db.session() as s:
            v = self._validate(s, data, product_id)
            if product_id:
                p = s.get(Product, product_id)
                if p is None:
                    raise NotFoundError("Product not found.")
                if expected_version is not None and p.version != expected_version:
                    raise ConcurrencyError("This product was changed by someone else. Reopen it and try again.")
                if v["unit_id"] != p.unit_id and self.has_movements(s, p.id):
                    raise ValidationError(
                        "The unit of measure cannot be changed after stock movements exist "
                        "(it would change the meaning of past quantities). Create a new product instead.",
                        "unit_id",
                    )
                if not v["is_active"] and p.is_active:
                    # Any bin with a non-zero balance blocks archiving - including +5 in one bin and
                    # -5 in another, which add up to zero but are both real problems to resolve first.
                    holding = s.scalar(select(func.count(StockLevel.id)).where(
                        StockLevel.product_id == p.id, StockLevel.quantity != ZERO))
                    if holding:
                        raise ValidationError(
                            f"Cannot archive {p.internal_code}: {holding} bin(s) still show stock for it "
                            f"(positive or negative). Issue, transfer or adjust it to zero first.", "is_active")
                old = audit.snapshot(p, PRODUCT_FIELDS)
                old_fin = audit.snapshot(p.finished_info, FINISHED_FIELDS) if p.finished_info else {}
            else:
                p = Product(created_by_id=self.user.id)
                s.add(p)
                old, old_fin = {}, {}
            for k, val in v.items():
                setattr(p, k, val)
            self._apply_finished(s, p, data)
            s.flush()
            new = audit.snapshot(p, PRODUCT_FIELDS)
            new_fin = audit.snapshot(p.finished_info, FINISHED_FIELDS) if p.finished_info else {}
            o, n = audit.diff({**old, **old_fin}, {**new, **new_fin})
            if n:
                audit.record(s, self.ctx, "PRODUCT_UPDATED" if product_id else "PRODUCT_CREATED", "PRODUCT", p.id,
                             f"{p.internal_code} {p.name}", old=o, new=n)
            saved_id = p.id
        return self.get(saved_id)

    def _apply_finished(self, s: Session, p: Product, data: dict) -> None:
        if p.item_type != ItemType.FINISHED.value:
            return
        fin = p.finished_info or FinishedProductInfo()
        fin.production_date = data.get("production_date") or None
        fin.site_id = data.get("site_id") or None
        fin.customer = clean_str(data.get("customer"), "customer", max_len=200)
        try:
            fin.status = FinishedStatus(data.get("status") or FinishedStatus.READY).value
        except ValueError:
            raise ValidationError("Choose a valid status.", "status") from None
        p.finished_info = fin

    def set_finished_status(self, product_id: int, status: str) -> None:
        self.require(P.PRODUCTS_EDIT)
        with self.db.session() as s:
            p = s.get(Product, product_id)
            if p is None or p.finished_info is None:
                raise NotFoundError("Finished product not found.")
            old = p.finished_info.status
            p.finished_info.status = FinishedStatus(status).value
            audit.record(s, self.ctx, "FINISHED_STATUS_CHANGED", "PRODUCT", p.id, p.internal_code,
                         old={"status": old}, new={"status": status})
