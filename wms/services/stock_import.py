"""Stock quantities from Excel (v1.38): each row's quantity is ADDED to the bin (Ihor, 22.11.2026).

The file is not posted directly. It becomes a stock import document (a stock count of kind
IMPORT_ADD) waiting for approval: in WMS now -> + quantity from the file -> after. A manager with
"Stock adjustments" approves it on the Stocktake screen, and only then is each quantity posted as
an Opening Balance adjustment (in bulk, ``StockEngine.post_many``). So:

* the same file cannot be added twice by mistake (its SHA-256 fingerprint is remembered);
* a stock list exported from WMS is refused (adding it would double the stock);
* rows with a problem (unknown code or bin, bad quantity, duplicate) are listed and left out.

Columns: Code (required), Bin (optional - the material's default bin is used when blank) and
Quantity (required, the amount to add).
"""

from __future__ import annotations

import hashlib
import re
import zipfile
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import lazyload

from wms.core.enums import CountStatus, EntityType
from wms.core.exceptions import ValidationError
from wms.core.numbers import QTY_PLACES, ZERO, fmt_datetime, utcnow
from wms.core.permissions import P
from wms.db.models import Bin, Product, StockCount, StockCountLine, StockLevel, Unit
from wms.services import audit
from wms.services.base import BaseService
from wms.services.numbering import next_number

MAX_HEADER_SCAN = 15
MAX_ROWS = 50_000

#: header words for each column (lower case, punctuation removed)
HEADERS = {
    "code": ("code", "internal code", "product code", "material code", "item code", "sku", "код", "артикул"),
    "bin": ("bin", "bin code", "location", "bin location", "комірка", "місце"),
    "qty": ("quantity", "qty", "quantity to add", "add", "on hand", "stock", "counted", "кількість", "залишок"),
}
#: titles WMS writes in cell A1 of its stock exports - adding one of those would double the stock
STOCK_EXPORTS = ("stock by bin", "current stock", "stock by location")

OK, SAME, PROBLEM = "OK", "SAME", "PROBLEM"
IMPORT_ADD = "IMPORT_ADD"


@dataclass
class StockImportRow:
    row: int  # the Excel row number
    code: str
    bin_code: str
    qty_text: str
    product_id: int | None = None
    product_name: str = ""
    unit: str = ""
    bin_id: int | None = None
    bin_full: str = ""
    qty: Decimal | None = None
    current: Decimal = ZERO
    problem: str | None = None
    warning: str | None = None  # used, but worth a look (no unit cost)

    @property
    def after(self) -> Decimal | None:
        return None if self.problem or self.qty is None else self.current + self.qty

    @property
    def status(self) -> str:
        if self.problem:
            return PROBLEM
        return SAME if self.qty == ZERO else OK


@dataclass
class StockImportPlan:
    path: Path
    sheet: str
    header_row: int
    columns: dict[str, int]  # "code" / "bin" / "qty" -> 0-based column
    rows: list[StockImportRow] = field(default_factory=list)
    sha256: str = ""
    #: an earlier import of this very file ("SC-000123 on 21/11/2026 14:05"), or None
    imported_before: str | None = None

    @property
    def good(self) -> list[StockImportRow]:
        return [r for r in self.rows if not r.problem]

    @property
    def problems(self) -> list[StockImportRow]:
        return [r for r in self.rows if r.problem]

    @property
    def changes(self) -> list[StockImportRow]:
        """Rows that add something."""
        return [r for r in self.rows if r.status == OK]

    def summary(self) -> str:
        n_add, n_prob = len(self.changes), len(self.problems)
        n_warn = sum(1 for r in self.good if r.warning)
        return (f"{len(self.rows):,} row(s): {n_add:,} will be added"
                + (f", {len(self.good) - n_add:,} with 0 (nothing to add)" if len(self.good) > n_add else "")
                + (f", {n_prob:,} with a problem (left out)" if n_prob else "")
                + (f", {n_warn:,} to check" if n_warn else "") + ".")


def _norm(text) -> str:
    return re.sub(r"[^a-z0-9а-яіїєґ ]+", " ", str(text or "").lower()).strip()


def _text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return " ".join(str(value).split())


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def find_columns(values: list) -> dict[str, int]:
    """Column per field for a row that looks like a header ('Code', 'Bin', 'Quantity' ...)."""
    out: dict[str, int] = {}
    normed = [_norm(v) for v in values]
    for exact in (True, False):
        for key, words in HEADERS.items():
            if key in out:
                continue
            for i, h in enumerate(normed):
                if not h or i in out.values():
                    continue
                if (h in words) if exact else any(len(w) > 3 and h.startswith(w) for w in words):
                    out[key] = i
                    break
    return out


def parse_qty(value) -> Decimal:
    if isinstance(value, bool):
        raise ValueError
    if isinstance(value, (int, float, Decimal)):
        d = Decimal(str(value))
    else:
        text = str(value).strip().replace(",", "").replace(" ", "")
        if not text:
            raise ValueError
        d = Decimal(text)
    if not d.is_finite():
        raise ValueError
    return d.quantize(QTY_PLACES)


class StockImportService(BaseService):
    def sheets(self, path: str | Path) -> list[str]:
        wb = self._open(path)
        try:
            return list(wb.sheetnames)
        finally:
            wb.close()

    def read(self, path: str | Path, sheet: str | None = None, progress=None) -> StockImportPlan:
        """Read the file and compare it with stock now. Nothing is changed."""
        self.require(P.STOCKTAKE_COUNT)
        path = Path(path)
        wb = self._open(path)
        try:
            ws = wb[sheet] if sheet else wb.worksheets[0]
            rows = ws.iter_rows(values_only=True)
            header_row, cols = 0, {}
            for i, values in enumerate(rows, start=1):
                if i == 1 and _norm(values[0] if values else "") in STOCK_EXPORTS:
                    raise ValidationError(
                        f"This is a stock list exported from WMS ('{_text(values[0])}'). Importing adds each "
                        "quantity to the bin, so it would double the stock. Use a file with the quantities to add.")
                found = find_columns(list(values))
                if "code" in found and "qty" in found:
                    header_row, cols = i, found
                    break
                if i >= MAX_HEADER_SCAN:
                    break
            if not cols:
                raise ValidationError(
                    f"Sheet '{ws.title}' has no header row with 'Code' and 'Quantity' columns (and optionally "
                    "'Bin').")
            plan = StockImportPlan(path, ws.title, header_row, cols, sha256=_sha256(path))
            width = max(cols.values()) + 1
            for n, values in enumerate(rows, start=header_row + 1):
                values = list(values) + [None] * (width - len(values))
                code, bin_code = _text(values[cols["code"]]), _text(values[cols["bin"]]) if "bin" in cols else ""
                raw_qty = values[cols["qty"]]
                if not code and raw_qty in (None, ""):
                    continue  # empty row, or a "Total" / note line
                if len(plan.rows) >= MAX_ROWS:
                    raise ValidationError(f"The sheet has more than {MAX_ROWS:,} rows - split it into smaller files.")
                r = StockImportRow(n, code, bin_code, _text(raw_qty))
                if not code:
                    r.problem = "No code"
                else:
                    try:
                        r.qty = parse_qty(raw_qty)
                        if r.qty < ZERO:
                            r.problem = "Quantity below zero"
                    except (ValueError, InvalidOperation):
                        r.problem = "Quantity is not a number" if r.qty_text else "No quantity"
                plan.rows.append(r)
                if progress and len(plan.rows) % 500 == 0:
                    progress(len(plan.rows), None)
        finally:
            wb.close()
        self._match(plan)
        return plan

    def _match(self, plan: StockImportPlan) -> None:
        """Find materials and bins (case does not matter) and today's quantity, in a few queries."""
        codes = {r.code.upper() for r in plan.rows if not r.problem}
        with self.db.read_session() as s:
            products = {}
            for i in range(0, len(codes), 900):
                chunk = list(codes)[i:i + 900]
                for p, unit_code, allow_dec, default_bin in s.execute(
                        select(Product, Unit.code, Unit.allow_decimal, Bin.full_code)
                        .options(lazyload("*"))
                        .join(Unit, Unit.id == Product.unit_id)
                        .outerjoin(Bin, Bin.id == Product.default_bin_id)
                        .where(func.upper(Product.internal_code).in_(chunk))):
                    products[p.internal_code.upper()] = (p, unit_code, allow_dec, default_bin)
            bins = {b.full_code.upper(): b for b in s.scalars(select(Bin).options(lazyload("*")))}
            seen: dict[tuple[int, int], int] = {}
            for r in plan.rows:
                if r.problem:
                    continue
                found = products.get(r.code.upper())
                if found is None:
                    r.problem = "Unknown code"
                    continue
                p, unit_code, allow_dec, default_bin = found
                r.product_id, r.product_name, r.unit = p.id, p.name, unit_code
                if not p.is_active:
                    r.problem = "Material is archived"
                    continue
                if not allow_dec and r.qty != r.qty.to_integral_value():
                    r.problem = f"Counted in whole {unit_code}"
                    continue
                bin_code = r.bin_code or default_bin or ""
                if not bin_code:
                    r.problem = "No bin, and the material has no default bin"
                    continue
                b = bins.get(bin_code.upper())
                if b is None:
                    r.problem = f"Unknown bin {bin_code}"
                    continue
                if not b.is_active:
                    r.problem = f"Bin {b.full_code} is not in use"
                    continue
                r.bin_id, r.bin_full = b.id, b.full_code
                key = (p.id, b.id)
                if key in seen:
                    r.problem = f"Same material and bin as row {seen[key]}"
                    continue
                seen[key] = r.row
            levels = self._levels(s, list(seen))
            costs = {v[0].id: v[0].unit_cost for v in products.values()}
            earlier = None
            if plan.sha256:
                earlier = s.execute(select(StockCount.count_number, StockCount.created_at)
                                    .where(StockCount.source_sha256 == plan.sha256,
                                           StockCount.status != CountStatus.CANCELLED.value)
                                    .order_by(StockCount.id.desc()).limit(1)).first()
        if earlier:
            plan.imported_before = f"{earlier[0]} on {fmt_datetime(earlier[1])}"
        for r in plan.rows:
            if r.problem:
                continue
            r.current = levels.get((r.product_id, r.bin_id), ZERO)
            if r.qty > ZERO and not costs.get(r.product_id):
                r.warning = "No unit cost: the added stock is valued at £0"

    @staticmethod
    def _levels(s, pairs) -> dict[tuple[int, int], Decimal]:
        from sqlalchemy import tuple_
        out = {}
        for i in range(0, len(pairs), 500):
            chunk = pairs[i:i + 500]
            out.update({(p, b): q for p, b, q in s.execute(
                select(StockLevel.product_id, StockLevel.bin_id, StockLevel.quantity)
                .where(tuple_(StockLevel.product_id, StockLevel.bin_id).in_(chunk)))})
        return out

    def create_count(self, plan: StockImportPlan, description: str | None = None,
                     allow_repeat: bool = False) -> StockCount:
        """A stock import document of the file's rows that add something, waiting for approval. Each
        line keeps "in WMS now" and "after" (= now + quantity); approval adds the difference."""
        self.require(P.STOCKTAKE_COUNT)
        good = plan.changes
        if not good:
            raise ValidationError("No row of the file adds anything - see the problems listed.")
        now = utcnow()
        with self.db.session() as s:
            if plan.sha256 and not allow_repeat:
                earlier = s.scalar(select(StockCount).where(StockCount.source_sha256 == plan.sha256,
                                                            StockCount.status != CountStatus.CANCELLED.value)
                                   .order_by(StockCount.id.desc()).limit(1))
                if earlier is not None:
                    raise ValidationError(f"This file was already imported as {earlier.count_number} on "
                                          f"{fmt_datetime(earlier.created_at)}. Importing it again would add the "
                                          "same quantities a second time.")
            levels = self._levels(s, [(r.product_id, r.bin_id) for r in good])
            count = StockCount(count_number=next_number(s, "STOCK_COUNT"), status=CountStatus.SUBMITTED.value,
                               kind=IMPORT_ADD, source_sha256=plan.sha256 or None,
                               description=(description or f"Excel import: {plan.path.name}")[:200],
                               notes=(f"Adds stock from {plan.path.name}, sheet '{plan.sheet}': {len(good)} row(s) "
                                      f"used, {len(plan.problems)} left out."),
                               created_by_id=self.user.id, submitted_by_id=self.user.id, submitted_at=now)
            s.add(count)
            s.flush()
            lines = []
            for i, r in enumerate(good, start=1):
                current = levels.get((r.product_id, r.bin_id), ZERO)
                lines.append(StockCountLine(count_id=count.id, line_no=i, product_id=r.product_id, bin_id=r.bin_id,
                                            system_qty=current, counted_qty=current + r.qty,
                                            counted_by_id=self.user.id, counted_at=now, notes=f"Excel row {r.row}"))
            s.add_all(lines)
            audit.record(s, self.ctx, "STOCK_IMPORTED", EntityType.STOCK_COUNT.value, count.id, count.count_number,
                         f"{len(good)} line(s) to add from {plan.path.name} (sheet '{plan.sheet}'), "
                         f"{len(plan.problems)} row(s) left out. Waiting for approval."
                         + (" Imported again on purpose." if allow_repeat and plan.imported_before else ""))
            count_id = count.id
        from wms.services.stocktake import StocktakeService
        return StocktakeService(self.ctx).get(count_id)

    @staticmethod
    def _open(path: str | Path):
        from openpyxl import load_workbook
        p = Path(path)
        if not p.is_file():
            raise ValidationError(f"File not found: {p}")
        if p.suffix.lower() not in (".xlsx", ".xlsm"):
            raise ValidationError("Choose an Excel workbook (.xlsx). Older .xls files: open them in Excel and "
                                  "'Save As' .xlsx first.")
        try:
            return load_workbook(p, read_only=True, data_only=True)
        except zipfile.BadZipFile:
            raise ValidationError(f"'{p.name}' is not a valid Excel workbook (or it is password protected).") \
                from None
        except PermissionError:
            raise ValidationError(f"'{p.name}' is open in another program. Close it and try again.") from None
