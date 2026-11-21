"""Stock quantities from Excel (v1.38): "set the quantity in each bin to what the file says".

The file is not posted directly. It becomes a stock count that is already counted and submitted:
system quantity (what WMS has now) -> counted quantity (the file) -> difference. A manager with
"Stock adjustments" reviews and approves it on the Stocktake screen, and only then are the
differences posted (in bulk, ``StockEngine.post_many``). So:

* importing the same file twice changes nothing the second time (the differences are zero);
* stock that moved between the import and the approval is caught by the normal stocktake check;
* rows with a problem (unknown code or bin, bad quantity, duplicate) are listed and left out.

Columns: Code (required), Bin (optional - the material's default bin is used when blank) and
Quantity (required). The "Stock by Bin" report exported to Excel is a ready-made file.
"""

from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import lazyload

from wms.core.enums import CountStatus, EntityType
from wms.core.exceptions import ValidationError
from wms.core.numbers import QTY_PLACES, ZERO, fmt_datetime, fmt_qty, utcnow
from wms.core.permissions import P
from wms.db.models import Bin, Product, StockCount, StockCountLine, StockLevel, StockMovement, Unit
from wms.services import audit
from wms.services.base import BaseService
from wms.services.numbering import next_number

MAX_HEADER_SCAN = 15
MAX_ROWS = 50_000

#: header words for each column (lower case, punctuation removed)
HEADERS = {
    "code": ("code", "internal code", "product code", "material code", "item code", "sku", "код", "артикул"),
    "bin": ("bin", "bin code", "location", "bin location", "комірка", "місце"),
    "qty": ("quantity", "qty", "on hand", "stock", "counted", "counted qty", "count", "кількість", "залишок"),
}

OK, SAME, PROBLEM = "OK", "SAME", "PROBLEM"
#: the line WMS writes above every exported report: "My Company Ltd - Generated 21/11/2026 08:00"
GENERATED = re.compile(r"Generated (\d{2}/\d{2}/\d{4} \d{2}:\d{2})")


def _local_to_utc(text: str) -> datetime | None:
    try:
        return datetime.strptime(text, "%d/%m/%Y %H:%M").astimezone(UTC)
    except ValueError:
        return None


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
    warning: str | None = None  # used, but worth a look (no unit cost, below project stock)

    @property
    def difference(self) -> Decimal | None:
        return None if self.problem or self.qty is None else self.qty - self.current

    @property
    def status(self) -> str:
        if self.problem:
            return PROBLEM
        return SAME if self.difference == ZERO else OK


@dataclass
class StockImportPlan:
    path: Path
    sheet: str
    header_row: int
    columns: dict[str, int]  # "code" / "bin" / "qty" -> 0-based column
    rows: list[StockImportRow] = field(default_factory=list)
    #: when WMS exported the file ("Generated dd/mm/yyyy hh:mm" above the header), UTC; None = unknown
    exported_at: datetime | None = None

    @property
    def good(self) -> list[StockImportRow]:
        return [r for r in self.rows if not r.problem]

    @property
    def problems(self) -> list[StockImportRow]:
        return [r for r in self.rows if r.problem]

    @property
    def changes(self) -> list[StockImportRow]:
        return [r for r in self.rows if r.status == OK]

    def summary(self) -> str:
        n_change, n_prob = len(self.changes), len(self.problems)
        n_warn = sum(1 for r in self.good if r.warning)
        return (f"{len(self.rows):,} row(s): {n_change:,} will change, {len(self.good) - n_change:,} already match"
                + (f", {n_prob:,} with a problem (left out)" if n_prob else "")
                + (f", {n_warn:,} to check" if n_warn else "") + ".")

    @property
    def export_note(self) -> str:
        if self.exported_at is None:
            return ("The file has no export time, so stock that moved since it was made cannot be checked: "
                    "delete the rows you did not count.")
        return f"Exported from WMS {fmt_datetime(self.exported_at)}: rows whose stock moved since then are left out."


def _norm(text) -> str:
    return re.sub(r"[^a-z0-9а-яіїєґ ]+", " ", str(text or "").lower()).strip()


def _text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return " ".join(str(value).split())


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
            header_row, cols, exported = 0, {}, None
            for i, values in enumerate(rows, start=1):
                for v in values:
                    m = GENERATED.search(str(v or ""))
                    if m and exported is None:
                        exported = _local_to_utc(m.group(1))
                found = find_columns(list(values))
                if "code" in found and "qty" in found:
                    header_row, cols = i, found
                    break
                if i >= MAX_HEADER_SCAN:
                    break
            if not cols:
                raise ValidationError(
                    f"Sheet '{ws.title}' has no header row with 'Code' and 'Quantity' columns (and optionally "
                    "'Bin'). Tip: export the 'Stock by Bin' report to Excel - it has the right columns.")
            plan = StockImportPlan(path, ws.title, header_row, cols, exported_at=exported)
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
            # bins where each material has stock now (a blank Bin must not double-count other bins)
            ids = [v[0].id for v in products.values()]
            holding: dict[int, set[str]] = {}
            for i in range(0, len(ids), 900):
                for pid, code in s.execute(select(StockLevel.product_id, Bin.full_code)
                                           .join(Bin, Bin.id == StockLevel.bin_id)
                                           .where(StockLevel.product_id.in_(ids[i:i + 900]),
                                                  StockLevel.quantity != ZERO)):
                    holding.setdefault(pid, set()).add(code.upper())
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
                if not r.bin_code and default_bin and len(holding.get(p.id, ())) > (
                        1 if default_bin.upper() in holding.get(p.id, ()) else 0):
                    n = len(holding[p.id])
                    r.problem = f"No bin given, and the material is in {n} bin(s) - give the bin"
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
            moved = self._moved_since(s, plan.exported_at, list(seen)) if plan.exported_at else set()
            costs = {v[0].id: v[0].unit_cost for v in products.values()}
            from wms.services.reservations import reserved_by_product
            pids = sorted({p for p, _ in seen})
            held = reserved_by_product(s, pids) if pids else {}
            on_hand = {}
            for i in range(0, len(pids), 900):
                on_hand.update(dict(s.execute(select(StockLevel.product_id, func.sum(StockLevel.quantity))
                                              .where(StockLevel.product_id.in_(pids[i:i + 900]))
                                              .group_by(StockLevel.product_id)).all()))
        change: dict[int, Decimal] = {}
        for r in plan.rows:
            if r.problem:
                continue
            r.current = levels.get((r.product_id, r.bin_id), ZERO)
            if (r.product_id, r.bin_id) in moved and r.qty != r.current:
                r.problem = f"Stock moved since the file was exported ({fmt_datetime(plan.exported_at)})"
                continue
            change[r.product_id] = change.get(r.product_id, ZERO) + (r.qty - r.current)
            if r.qty > r.current and not costs.get(r.product_id):
                r.warning = "No unit cost: the added stock is valued at £0"
        for r in plan.rows:
            if r.problem or r.product_id not in held:
                continue
            after = (on_hand.get(r.product_id) or ZERO) + change.get(r.product_id, ZERO)
            if after < held[r.product_id] and r.qty < r.current:
                r.warning = (f"On hand would be {fmt_qty(after)}, below project stock {fmt_qty(held[r.product_id])}")

    @staticmethod
    def _moved_since(s, since: datetime, pairs) -> set[tuple[int, int]]:
        """(material, bin) pairs with any movement recorded after ``since``."""
        wanted, out = set(pairs), set()
        pids = sorted({p for p, _ in pairs})
        for i in range(0, len(pids), 900):
            for pid, fb, tb in s.execute(select(StockMovement.product_id, StockMovement.from_bin_id,
                                                StockMovement.to_bin_id)
                                         .where(StockMovement.recorded_at > since,
                                                StockMovement.product_id.in_(pids[i:i + 900]))):
                out.update(k for k in ((pid, fb), (pid, tb)) if k in wanted)
        return out

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

    def create_count(self, plan: StockImportPlan, description: str | None = None) -> StockCount:
        """A stock count made of the file's good rows, already counted and submitted for approval.
        The system quantity is read again now, so the count compares with what is in WMS today."""
        self.require(P.STOCKTAKE_COUNT)
        good = plan.good
        if not good:
            raise ValidationError("No row of the file can be used - see the problems listed.")
        now = utcnow()
        with self.db.session() as s:
            levels = self._levels(s, [(r.product_id, r.bin_id) for r in good])
            count = StockCount(count_number=next_number(s, "STOCK_COUNT"), status=CountStatus.SUBMITTED.value,
                               description=(description or f"Excel import: {plan.path.name}")[:200],
                               notes=(f"From {plan.path.name}, sheet '{plan.sheet}': {len(good)} row(s) used, "
                                      f"{len(plan.problems)} left out."),
                               created_by_id=self.user.id, submitted_by_id=self.user.id, submitted_at=now)
            s.add(count)
            s.flush()
            s.add_all([StockCountLine(count_id=count.id, line_no=i, product_id=r.product_id, bin_id=r.bin_id,
                                      system_qty=levels.get((r.product_id, r.bin_id), ZERO), counted_qty=r.qty,
                                      counted_by_id=self.user.id, counted_at=now, notes=f"Excel row {r.row}")
                       for i, r in enumerate(good, start=1)])
            audit.record(s, self.ctx, "STOCK_COUNT_IMPORTED", EntityType.STOCK_COUNT.value, count.id,
                         count.count_number, f"{len(good)} line(s) from {plan.path.name} (sheet '{plan.sheet}'), "
                         f"{len(plan.problems)} row(s) left out. Waiting for approval.")
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
