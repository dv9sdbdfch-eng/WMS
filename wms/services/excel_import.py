"""Excel import of the existing materials list: products, suppliers and product photos.

Workflow (spec section 23): select the workbook -> select the worksheet -> map the columns ->
preview -> validate -> resolve duplicates -> import. Nothing reaches the database before the
person has seen the preview, and the import itself:

* takes a safety backup first (section 30) and stops if that fails;
* writes every product, supplier, category and photo in ONE transaction - all or nothing;
* copies every photo into the WMS attachment store (section 24), so the workbook and any picture
  folder can be deleted afterwards.

Photos are found in four ways (section 24): pictures embedded in the sheet (floating over a cell or
placed in a cell with Excel's "Place in Cell"), a full file path in a cell, a hyperlink to a file,
or just a file name looked up in a chosen picture folder. Web addresses are reported, not
downloaded: the warehouse PC may be offline and a web page is not a picture.
"""

from __future__ import annotations

import posixpath
import re
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

from sqlalchemy import func, select

from wms.core.enums import EntityType, ItemType
from wms.core.exceptions import ValidationError, WMSError
from wms.core.numbers import ZERO
from wms.core.permissions import P
from wms.db.models import Attachment, Bin, Category, Product, Supplier, Unit
from wms.services import audit
from wms.services.attachments import PHOTO_EXT, AttachmentService, StagedFile
from wms.services.base import BaseService, normalise_code
from wms.services.products import ProductService

# --- what can be imported ------------------------------------------------------------------

#: field -> (label, header words that suggest it). Order = order on the mapping screen.
FIELDS: dict[str, tuple[str, tuple[str, ...]]] = {
    "internal_code": ("Code *", ("code", "internal code", "product code", "item code", "our code", "sku",
                                 "material code", "stock code", "код")),
    "name": ("Name *", ("name", "product name", "item name", "material", "material name", "product", "назва")),
    "description": ("Description", ("description", "desc", "details", "опис", "name / description")),
    "supplier_code": ("Product Number (supplier's code)", (
        "product number", "product no", "product no.", "supplier code", "supplier product code", "part number",
        "part no", "part no.", "article", "article number", "ref", "reference", "decor", "decor code", "артикул")),
    "supplier": ("Supplier", ("supplier", "vendor", "supplier name", "постачальник")),
    "barcode": ("Barcode", ("barcode", "ean", "ean13", "gtin", "upc", "штрихкод")),
    "brand": ("Brand", ("brand", "manufacturer", "make", "бренд")),
    "category": ("Category", ("category", "group", "product group", "категорія")),
    "unit": ("Unit", ("unit", "uom", "unit of measure", "units", "од")),
    "min_stock": ("Minimum stock", ("min", "minimum", "min stock", "minimum stock", "min level")),
    "reorder_level": ("Reorder level", ("reorder level", "reorder point")),
    "reorder_qty": ("Reorder quantity", ("reorder qty", "reorder quantity", "order qty")),
    "unit_cost": ("Unit cost (£)", ("cost", "unit cost", "price", "unit price", "cost price", "£", "ціна")),
    "default_bin": ("Default bin", ("bin", "location", "default bin", "комірка")),
    "photo": ("Photo", ("photo", "image", "picture", "pic", "product photo", "фото", "img")),
    "notes": ("Notes", ("notes", "note", "comment", "comments", "примітки")),
}
REQUIRED = ("internal_code", "name")
QTY_FIELDS = ("min_stock", "reorder_level", "reorder_qty")

CREATE, UPDATE, SKIP = "CREATE", "UPDATE", "SKIP"
ACTION_LABELS = {CREATE: "New", UPDATE: "Update existing", SKIP: "Skip"}

MAX_HEADER_SCAN = 15


# --- results of looking at a workbook ----------------------------------------------------------

@dataclass
class SheetInfo:
    name: str
    rows: int
    header_row: int  # 1-based
    headers: list[str]  # column titles of the header row ("Column C" when blank)
    samples: list[list[str]]  # first rows under the header, as text
    guess: dict[str, int]  # field -> 0-based column
    floating_images: int = 0
    cell_images: int = 0
    hyperlinks: int = 0
    paths: int = 0
    filenames: int = 0
    web_links: int = 0

    @property
    def photo_summary(self) -> str:
        parts = [f"{n} {what}" for n, what in (
            (self.floating_images, "picture(s) embedded over cells"), (self.cell_images, "picture(s) placed in cells"),
            (self.hyperlinks, "hyperlink(s) to files"), (self.paths, "file path(s)"),
            (self.filenames, "file name(s)"), (self.web_links, "web address(es) - not downloaded")) if n]
        return ", ".join(parts) if parts else "No photos found"


@dataclass
class WorkbookInfo:
    path: Path
    sheets: list[SheetInfo]

    def sheet(self, name: str) -> SheetInfo:
        return next(s for s in self.sheets if s.name == name)


@dataclass
class ImportOptions:
    sheet: str
    header_row: int
    mapping: dict[str, int]  # field -> 0-based column
    photo_folder: Path | None = None
    default_unit_id: int | None = None
    item_type: str = ItemType.RAW_MATERIAL.value
    on_existing: str = SKIP  # what to do with codes already in the WMS: SKIP / UPDATE
    create_suppliers: bool = True
    create_categories: bool = True


@dataclass
class PhotoSource:
    kind: str  # EMBEDDED / IN_CELL / PATH / HYPERLINK / FILENAME
    label: str  # what the person sees ("embedded picture", the path, the file name)
    path: Path | None = None  # file on disk (paths, hyperlinks, file names)
    data: bytes | None = None  # embedded picture bytes
    ext: str = ".png"


@dataclass
class ImportRow:
    row: int  # Excel row number
    values: dict[str, str]
    action: str = CREATE
    existing_id: int | None = None
    existing_label: str = ""
    photo: PhotoSource | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def code(self) -> str:
        return self.values.get("internal_code", "")

    @property
    def problems(self) -> str:
        return "; ".join(self.errors + self.warnings)


@dataclass
class ImportPlan:
    source: Path
    options: ImportOptions
    rows: list[ImportRow]

    @property
    def to_import(self) -> list[ImportRow]:
        return [r for r in self.rows if r.ok and r.action in (CREATE, UPDATE)]

    def counts(self) -> dict[str, int]:
        return {"rows": len(self.rows), "create": sum(1 for r in self.to_import if r.action == CREATE),
                "update": sum(1 for r in self.to_import if r.action == UPDATE),
                "skip": sum(1 for r in self.rows if r.ok and r.action == SKIP),
                "errors": sum(1 for r in self.rows if not r.ok),
                "photos": sum(1 for r in self.to_import if r.photo is not None)}

    def summary(self) -> str:
        c = self.counts()
        return (f"{c['rows']} row(s): {c['create']} new, {c['update']} to update, {c['skip']} skipped, "
                f"{c['errors']} with errors (not imported), {c['photos']} photo(s)")


@dataclass
class ImportResult:
    created: int
    updated: int
    skipped: int
    failed_rows: int
    photos: int
    suppliers_created: list[str]
    categories_created: list[str]
    backup: Path | None
    photo_problems: list[str] = field(default_factory=list)


# --- reading cells ----------------------------------------------------------------------------

def cell_text(value) -> str:
    """A cell as the text a person would type: 3311.0 -> '3311', dates as dd/mm/yyyy."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else repr(value)
    if hasattr(value, "strftime"):
        return value.strftime("%d/%m/%Y")
    return " ".join(str(value).split())


def _norm_header(text: str) -> str:
    return re.sub(r"[^a-z0-9£а-яіїєґ /]+", " ", text.lower()).strip()


def guess_mapping(headers: list[str]) -> dict[str, int]:
    """Field -> column for headers that obviously name a field (exact words first, then 'contains')."""
    out: dict[str, int] = {}
    used: set[int] = set()
    normed = [_norm_header(h) for h in headers]
    for exact in (True, False):
        for f, (_label, words) in FIELDS.items():
            if f in out:
                continue
            for i, h in enumerate(normed):
                if i in used or not h:
                    continue
                if (h in words) if exact else any(len(w) > 3 and w in h for w in words):
                    out[f] = i
                    used.add(i)
                    break
    # "Name / Description" in one column: it is the name
    if "name" not in out and "description" in out:
        out["name"] = out.pop("description")
    return out


def _is_header(values: list) -> bool:
    texts = [cell_text(v) for v in values if v not in (None, "")]
    return len(texts) >= 2 and len(guess_mapping(texts)) >= 1 and all(not isinstance(v, (int, float))
                                                                      for v in values if v not in (None, ""))


def _classify(text: str) -> str | None:
    t = text.strip()
    if not t:
        return None
    low = t.lower()
    if low.startswith(("http://", "https://", "www.")):
        return "WEB"
    if low.startswith("file:"):
        return "PATH"
    ext = Path(t.replace("\\", "/")).suffix.lower()
    if ext not in PHOTO_EXT:
        return None
    if "/" in t or "\\" in t:
        return "PATH"
    return "FILENAME"


# --- pictures inside the workbook ---------------------------------------------------------------

def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _rels(z: zipfile.ZipFile, part: str) -> dict[str, str]:
    """rId -> target path (inside the zip) for a part's relationships."""
    folder, name = posixpath.split(part)
    rel_part = posixpath.join(folder, "_rels", name + ".rels")
    if rel_part not in z.namelist():
        return {}
    out = {}
    for el in ET.fromstring(z.read(rel_part)):
        target = el.get("Target", "")
        out[el.get("Id")] = (target.lstrip("/") if target.startswith("/")
                             else posixpath.normpath(posixpath.join(folder, target)))
    return out


def _sheet_parts(z: zipfile.ZipFile) -> dict[str, str]:
    """Worksheet name -> its part inside the zip."""
    rels = _rels(z, "xl/workbook.xml")
    out = {}
    for el in ET.fromstring(z.read("xl/workbook.xml")).iter():
        if _local(el.tag) == "sheet":
            rid = next((v for k, v in el.attrib.items() if _local(k) == "id"), None)
            if rid in rels:
                out[el.get("name")] = rels[rid]
    return out


def in_cell_pictures(path: Path) -> dict[str, dict[tuple[int, int], tuple[bytes, str]]]:
    """Pictures placed IN cells (Excel 365 "Place in Cell" / IMAGE()): sheet -> (row, col) ->
    (bytes, extension). openpyxl does not read these, so the rich-value parts are followed by hand:
    cell vm -> valueMetadata -> futureMetadata rvb -> rich value -> richValueRel -> media file."""
    out: dict[str, dict[tuple[int, int], tuple[bytes, str]]] = {}
    try:
        z = zipfile.ZipFile(path)
    except zipfile.BadZipFile:
        return out
    with z:
        names = set(z.namelist())
        rv_part, rel_part, meta_part = ("xl/richData/rdrichvalue.xml", "xl/richData/richValueRel.xml",
                                        "xl/metadata.xml")
        if not {rv_part, rel_part, meta_part} <= names:
            return out
        meta = ET.fromstring(z.read(meta_part))
        future: list[int | None] = []  # futureMetadata XLRICHVALUE bk -> rich value index
        value_meta: list[int | None] = []  # valueMetadata bk (vm - 1) -> futureMetadata index
        types = [el.get("name") for el in meta.iter() if _local(el.tag) == "metadataType"]
        for el in meta:
            if _local(el.tag) == "futureMetadata" and el.get("name") == "XLRICHVALUE":
                for bk in el:
                    rvb = next((x for x in bk.iter() if _local(x.tag) == "rvb"), None)
                    future.append(int(rvb.get("i")) if rvb is not None else None)
            elif _local(el.tag) == "valueMetadata":
                for bk in el:
                    rc = next((x for x in bk if _local(x.tag) == "rc"), None)
                    ok = rc is not None and 0 < int(rc.get("t", 0)) <= len(types) and \
                        types[int(rc.get("t")) - 1] == "XLRICHVALUE"
                    value_meta.append(int(rc.get("v")) if ok else None)
        # rich values: which "v" is the picture (structure key _rvRel:LocalImageIdentifier)
        key_pos: dict[int, int] = {}
        struct_part = "xl/richData/rdrichvaluestructure.xml"
        if struct_part in names:
            for i, st in enumerate(x for x in ET.fromstring(z.read(struct_part)) if _local(x.tag) == "s"):
                keys = [k.get("n") for k in st if _local(k.tag) == "k"]
                if "_rvRel:LocalImageIdentifier" in keys:
                    key_pos[i] = keys.index("_rvRel:LocalImageIdentifier")
        rich: list[int | None] = []
        for rv in (x for x in ET.fromstring(z.read(rv_part)) if _local(x.tag) == "rv"):
            vals = [v.text for v in rv if _local(v.tag) == "v"]
            pos = key_pos.get(int(rv.get("s", 0)), 0)
            try:
                rich.append(int(vals[pos]))
            except (IndexError, TypeError, ValueError):
                rich.append(None)
        rel_ids = [next((v for k, v in el.attrib.items() if _local(k) == "id"), None)
                   for el in ET.fromstring(z.read(rel_part)) if _local(el.tag) == "rel"]
        targets = _rels(z, rel_part)

        def picture(vm: int) -> tuple[bytes, str] | None:
            try:
                f = value_meta[vm - 1]
                r = future[f] if f is not None else None
                rel = rich[r] if r is not None else None
                target = targets.get(rel_ids[rel]) if rel is not None else None
            except IndexError:
                return None
            if not target or target not in names:
                return None
            return z.read(target), posixpath.splitext(target)[1].lower() or ".png"

        for sheet, part in _sheet_parts(z).items():
            if part not in names:
                continue
            found: dict[tuple[int, int], tuple[bytes, str]] = {}
            for _ev, el in ET.iterparse(z.open(part)):
                if _local(el.tag) == "c" and el.get("vm"):
                    pic = picture(int(el.get("vm")))
                    if pic:
                        row, col = _ref(el.get("r", ""))
                        found[(row, col)] = pic
                    el.clear()
            if found:
                out[sheet] = found
    return out


def _ref(ref: str) -> tuple[int, int]:
    """'E12' -> (12, 4): Excel row, 0-based column."""
    m = re.match(r"([A-Z]+)(\d+)", ref)
    if not m:
        return 0, 0
    col = 0
    for ch in m.group(1):
        col = col * 26 + ord(ch) - 64
    return int(m.group(2)), col - 1


def floating_pictures(ws) -> dict[int, list[tuple[int, bytes, str]]]:
    """Pictures floating over the sheet, by the row of their top-left corner: row -> [(col, bytes, ext)]."""
    out: dict[int, list[tuple[int, bytes, str]]] = {}
    for img in getattr(ws, "_images", []):
        start = getattr(getattr(img, "anchor", None), "_from", None)
        if start is None:
            continue  # absolute position - no row to belong to
        try:
            data = img._data()
        except Exception:  # noqa: BLE001 - one unreadable picture must not stop the import
            continue
        ext = "." + (getattr(img, "format", None) or "png").lower().replace("jpeg", "jpg")
        out.setdefault(start.row + 1, []).append((start.col, data, ext))
    return out


# --- the service ------------------------------------------------------------------------------

class ExcelImportService(BaseService):

    def inspect(self, path: str | Path) -> WorkbookInfo:
        """Look at every worksheet: header row, columns, a few sample rows, the guessed mapping and
        how the photos are stored. Nothing is changed."""
        self.require(P.PRODUCTS_EDIT)
        path = self._check_file(path)
        wb = self._load(path)
        cells = in_cell_pictures(path)
        sheets = []
        for ws in wb.worksheets:
            rows = list(ws.iter_rows(min_row=1, max_row=MAX_HEADER_SCAN, values_only=True))
            header_row = next((i + 1 for i, r in enumerate(rows) if _is_header(list(r))), None)
            if header_row is None:
                header_row = next((i + 1 for i, r in enumerate(rows) if any(v not in (None, "") for v in r)), 1)
            headers = self._headers(ws, header_row)
            guess = guess_mapping(headers)
            samples = self._samples(ws, header_row, len(headers))
            info = SheetInfo(ws.title, max(ws.max_row - header_row, 0), header_row, headers, samples, guess,
                             floating_images=sum(len(v) for v in floating_pictures(ws).values()),
                             cell_images=len(cells.get(ws.title, {})))
            for row in ws.iter_rows(min_row=header_row + 1):
                for c in row:
                    if c.hyperlink is not None and c.hyperlink.target:
                        kind = _classify(c.hyperlink.target)
                        if kind == "WEB":
                            info.web_links += 1
                        elif kind:
                            info.hyperlinks += 1
                        continue
                    if isinstance(c.value, str):
                        kind = _classify(c.value)
                        if kind == "PATH":
                            info.paths += 1
                        elif kind == "FILENAME":
                            info.filenames += 1
                        elif kind == "WEB":
                            info.web_links += 1
            if "photo" not in guess:
                # A column made of file names / paths is the photo column even without a "Photo" title.
                for i in range(len(headers)):
                    col = [r[i] for r in samples if i < len(r) and r[i]]
                    if col and all(_classify(v) in ("PATH", "FILENAME") for v in col) and i not in guess.values():
                        guess["photo"] = i
                        break
            sheets.append(info)
        wb.close()
        return WorkbookInfo(path, sheets)

    def read_sheet(self, path: str | Path, sheet: str, header_row: int) -> SheetInfo:
        """Column titles, sample rows and the guessed mapping when the person picks another header row."""
        self.require(P.PRODUCTS_EDIT)
        path = self._check_file(path)
        wb = self._load(path)
        try:
            ws = wb[sheet]
            headers = self._headers(ws, header_row)
            samples = self._samples(ws, header_row, len(headers))
            return SheetInfo(ws.title, max(ws.max_row - header_row, 0), header_row, headers, samples,
                             guess_mapping(headers))
        finally:
            wb.close()

    # -- preview and validation ----------------------------------------------------------------
    def prepare(self, path: str | Path, options: ImportOptions) -> ImportPlan:
        """Read the rows with the chosen mapping and check every one against the database.
        Nothing is changed."""
        self.require(P.PRODUCTS_EDIT)
        path = self._check_file(path)
        # A "Name / Description" column alone is enough: the name is taken from it.
        missing = [FIELDS[f][0].rstrip(" *") for f in REQUIRED if f not in options.mapping
                   and not (f == "name" and "description" in options.mapping)]
        if missing:
            raise ValidationError("Choose the column for: " + ", ".join(missing) + ".")
        wb = self._load(path)
        if options.sheet not in wb.sheetnames:
            raise ValidationError(f"The workbook has no worksheet '{options.sheet}'.")
        ws = wb[options.sheet]
        floating = floating_pictures(ws)
        in_cell = in_cell_pictures(path).get(options.sheet, {})
        index = PhotoFolder(options.photo_folder)
        rows: list[ImportRow] = []
        photo_col = options.mapping.get("photo")
        for cells in ws.iter_rows(min_row=options.header_row + 1):
            if not cells:
                continue
            excel_row = cells[0].row
            values = {f: cell_text(cells[c].value) if c < len(cells) else "" for f, c in options.mapping.items()}
            if not any(values.values()) and excel_row not in floating:
                continue  # blank line
            if values.get("internal_code", "").strip().upper() in ("TOTAL", "TOTALS", "GRAND TOTAL") \
                    and not values.get("name"):
                continue  # the totals line under an exported list
            r = ImportRow(excel_row, values)
            r.photo = self._photo_for(r, cells, photo_col, floating.get(excel_row, []), in_cell, path, index)
            rows.append(r)
        wb.close()
        self._validate(rows, options)
        return ImportPlan(path, options, rows)

    def _photo_for(self, r: ImportRow, cells, photo_col, floating, in_cell, workbook: Path,
                   index: PhotoFolder) -> PhotoSource | None:
        # 1. a picture placed in the photo cell, or floating over that row
        if photo_col is not None and (r.row, photo_col) in in_cell:
            data, ext = in_cell[(r.row, photo_col)]
            return self._embedded(r, "IN_CELL", "picture in cell", data, ext)
        row_cells = {(row, col): v for (row, col), v in in_cell.items() if row == r.row}
        if row_cells:
            data, ext = next(iter(row_cells.values()))
            return self._embedded(r, "IN_CELL", "picture in cell", data, ext)
        if floating:
            pick = next((p for p in floating if p[0] == photo_col), floating[0])
            if len(floating) > 1:
                r.warnings.append(f"{len(floating)} pictures on this row - the one in the photo column (or the "
                                  f"first) is used")
            return self._embedded(r, "EMBEDDED", "embedded picture", pick[1], pick[2])
        if photo_col is None or photo_col >= len(cells):
            return None
        cell = cells[photo_col]
        link = cell.hyperlink.target if cell.hyperlink is not None and cell.hyperlink.target else None
        text = link or (cell.value if isinstance(cell.value, str) else "")
        kind = _classify(text or "")
        if not text:
            return None
        if kind == "WEB":
            r.warnings.append("photo is a web address - not downloaded (save the pictures to a folder and use "
                              "file names)")
            return None
        if kind is None:
            r.warnings.append(f"photo '{text}' is not a picture file (jpg / png / gif / bmp / webp)")
            return None
        found = index.find(text, workbook.parent)
        if found is None:
            r.warnings.append(f"photo file not found: {text}")
            return None
        try:
            AttachmentService(self.ctx).validate_file(found)  # damaged / renamed / too big: say so now
        except ValidationError as exc:
            r.warnings.append(f"photo not used: {exc.message}")
            return None
        return PhotoSource("HYPERLINK" if link else kind, text, path=found, ext=found.suffix.lower())

    @staticmethod
    def _embedded(r: ImportRow, kind: str, label: str, data: bytes, ext: str) -> PhotoSource | None:
        ext = ".jpg" if ext == ".jpeg" else ext
        if ext not in PHOTO_EXT:
            r.warnings.append(f"embedded picture is a {ext[1:].upper()} drawing, not a photo - not imported")
            return None
        return PhotoSource(kind, label, data=data, ext=ext)

    def _validate(self, rows: list[ImportRow], o: ImportOptions) -> None:
        with self.db.read_session() as s:
            units = {u.code.upper(): u for u in s.scalars(select(Unit))}
            units.update({u.name.upper(): u for u in units.values()})
            if o.default_unit_id is None and "unit" not in o.mapping:
                raise ValidationError("Choose the unit of measure for products without a Unit column.")
            default_unit = s.get(Unit, o.default_unit_id) if o.default_unit_id else None
            existing = {p.internal_code.upper(): p for p in s.scalars(select(Product)).unique()}
            barcodes = {p.barcode.upper(): p for p in existing.values() if p.barcode}
            suppliers = {x.name.upper(): x for x in s.scalars(select(Supplier))}
            suppliers.update({x.code.upper(): x for x in suppliers.values()})
            categories = {n.upper() for c in s.scalars(select(Category)).unique() for n in (c.name, c.full_name)}
            bins = {}
            for b in s.scalars(select(Bin).where(Bin.is_active.is_(True))):
                bins.setdefault(b.code.upper(), []).append(b)
                bins.setdefault(b.full_code.upper(), []).append(b)
                if b.barcode:
                    bins.setdefault(b.barcode.upper(), []).append(b)
            by_supplier_code = {(p.supplier_id, (p.supplier_code or "").upper()): p for p in existing.values()
                                if p.supplier_code}
            seen: dict[str, ImportRow] = {}
            seen_barcodes: dict[str, ImportRow] = {}
            for r in rows:
                v = r.values
                code = normalise_code(v.get("internal_code"))
                v["internal_code"] = code
                if not v.get("name") and v.get("description"):
                    v["name"] = v["description"][:200]
                    if "description" in o.mapping and o.mapping.get("name") is None:
                        v["description"] = "" if len(v["description"]) <= 200 else v["description"]
                if not code:
                    r.errors.append("no code")
                elif len(code) > 50:
                    r.errors.append("code longer than 50 characters")
                if not v.get("name"):
                    r.errors.append("no name")
                elif len(v["name"]) > 200:
                    r.warnings.append("name cut to 200 characters")
                    v["name"] = v["name"][:200]
                # unit
                unit_text = (v.get("unit") or "").upper()
                unit = units.get(unit_text) if unit_text else default_unit
                if unit is None:
                    r.errors.append(f"unknown unit '{v.get('unit')}'" if unit_text else "no unit")
                else:
                    v["unit_id"] = str(unit.id)
                # numbers
                for f in (*QTY_FIELDS, "unit_cost"):
                    raw = (v.get(f) or "").replace("£", "").replace(",", "").strip()
                    if not raw:
                        v[f] = ""
                        continue
                    try:
                        num = Decimal(raw)
                    except InvalidOperation:
                        r.errors.append(f"{FIELDS[f][0]}: '{v.get(f)}' is not a number")
                        continue
                    if num < ZERO:
                        r.errors.append(f"{FIELDS[f][0]} cannot be negative")
                    elif f in QTY_FIELDS and unit is not None and not unit.allow_decimal and num != num.to_integral():
                        r.errors.append(f"{FIELDS[f][0]}: {unit.code} must be a whole number")
                    v[f] = str(num)
                # supplier / category / bin
                sup_text = (v.get("supplier") or "").strip()
                sup = suppliers.get(sup_text.upper()) if sup_text else None
                if sup_text and sup is None:
                    if o.create_suppliers:
                        r.warnings.append(f"new supplier '{sup_text}' will be created")
                    else:
                        r.errors.append(f"unknown supplier '{sup_text}'")
                elif sup is not None and not sup.is_active:
                    r.warnings.append(f"supplier {sup.name} is archived")
                cat_text = (v.get("category") or "").strip()
                if cat_text and cat_text.upper() not in categories:
                    if o.create_categories:
                        r.warnings.append(f"new category '{cat_text}' will be created")
                    else:
                        r.errors.append(f"unknown category '{cat_text}'")
                bin_text = (v.get("default_bin") or "").strip().upper()
                if bin_text:
                    found = bins.get(bin_text, [])
                    if len({b.id for b in found}) != 1:
                        r.warnings.append(f"bin '{v['default_bin']}' {'is ambiguous' if found else 'not found'} "
                                          f"- left empty")
                        v["default_bin"] = ""
                    else:
                        v["default_bin_id"] = str(found[0].id)
                # duplicates: inside the file
                if code:
                    first = seen.get(code)
                    if first is not None:
                        r.errors.append(f"code {code} is also on row {first.row}")
                        r.action = SKIP
                    else:
                        seen[code] = r
                bc = (v.get("barcode") or "").upper()
                if bc:
                    other = seen_barcodes.get(bc)
                    if other is not None:
                        r.errors.append(f"barcode {v['barcode']} is also on row {other.row}")
                    seen_barcodes[bc] = r
                # duplicates: already in the WMS
                match = existing.get(code) if code else None
                if match is not None:
                    r.existing_id = match.id
                    r.existing_label = f"{match.internal_code} - {match.name}"
                    if not match.is_active:
                        r.action = SKIP
                        r.warnings.append(f"{match.internal_code} exists but was removed from active use - "
                                          f"restore it first to update it")
                    else:
                        r.action = o.on_existing if o.on_existing in (SKIP, UPDATE) else SKIP
                        if unit is not None and match.unit_id != unit.id and r.action == UPDATE:
                            r.warnings.append(f"unit stays {match.unit.code} (not changed by import)")
                elif r.ok:
                    r.action = CREATE
                if bc and bc in barcodes and barcodes[bc].id != r.existing_id:
                    r.errors.append(f"barcode {v['barcode']} already belongs to {barcodes[bc].internal_code}")
                if bc and bc in existing and existing[bc].id != r.existing_id:
                    r.errors.append(f"barcode {v['barcode']} is the code of product {bc}")
                if code and code in barcodes and barcodes[code].id != r.existing_id:
                    r.errors.append(f"{code} is the barcode of product {barcodes[code].internal_code}")
                sc = (v.get("supplier_code") or "").upper()
                if sc and sup is not None and r.existing_id is None:
                    twin = by_supplier_code.get((sup.id, sc))
                    if twin is not None:
                        r.warnings.append(f"same supplier code as {twin.internal_code} - possible duplicate")
                if not r.ok:
                    r.action = SKIP

    # -- the import ---------------------------------------------------------------------------
    def run(self, plan: ImportPlan) -> ImportResult:
        """Import the rows marked New / Update. Backup first; then everything in one transaction."""
        self.require(P.PRODUCTS_EDIT)
        rows = plan.to_import
        if not rows:
            raise ValidationError("Nothing to import: every row is skipped or has errors.")
        for r in rows:
            if (r.action == UPDATE) != (r.existing_id is not None):
                raise ValidationError(f"Row {r.row} ({r.code}): only a code already in the WMS can be updated, "
                                      f"and only a new code can be created. Check the preview again.")
        with_photos = [r for r in rows if r.photo is not None]
        if with_photos:
            self.require(P.ATTACHMENTS_ADD)
        from wms.services.backup import BackupService
        try:
            backup = BackupService(self.ctx).create_safety(
                f"Pre-import backup before Excel import of {plan.source.name} ({len(rows)} products)")
        except WMSError:
            raise
        except Exception as exc:  # noqa: BLE001 - no backup, no import
            raise ValidationError(f"The pre-import backup failed, so nothing was imported: {exc}") from exc
        att = AttachmentService(self.ctx)
        staged: dict[int, StagedFile] = {}
        photo_problems: list[str] = []
        with tempfile.TemporaryDirectory() as tmp:
            for r in with_photos:
                src = r.photo.path
                if src is None:
                    src = Path(tmp) / f"row{r.row}_{r.code or 'photo'}{r.photo.ext}"
                    src = src.with_name(re.sub(r"[^A-Za-z0-9._-]+", "_", src.name))
                    src.write_bytes(r.photo.data)
                try:
                    staged[r.row] = att.stage(src)
                except WMSError as exc:
                    photo_problems.append(f"Row {r.row} ({r.code}): {exc.message}")
        products = ProductService(self.ctx)
        sup_new: dict[str, Supplier] = {}
        cat_new: dict[str, Category] = {}
        created = updated = photos = 0
        try:
            with self.db.session() as s:
                suppliers = {x.name.upper(): x for x in s.scalars(select(Supplier))}
                suppliers.update({x.code.upper(): x for x in list(suppliers.values())})
                categories = {n.upper(): c for c in s.scalars(select(Category)).unique() for n in (c.name, c.full_name)}
                for r in rows:
                    v = r.values
                    try:
                        data = self._product_data(s, r, plan.options, suppliers, sup_new, categories, cat_new)
                        p = products.save_in(s, r.existing_id if r.action == UPDATE else None, data)
                    except WMSError as exc:
                        raise ValidationError(f"Row {r.row} ({v.get('internal_code')}): {exc.message} "
                                              f"- nothing was imported.") from exc
                    if r.action == UPDATE:
                        updated += 1
                    else:
                        created += 1
                    f = staged.get(r.row)
                    if f is not None:
                        same = s.scalar(select(func.count(Attachment.id)).where(
                            Attachment.entity_type == EntityType.PRODUCT.value, Attachment.entity_id == p.id,
                            Attachment.sha256 == f.sha256, Attachment.is_deleted.is_(False)))
                        if same:
                            att.discard([f])
                            staged.pop(r.row)
                            continue
                        att.attach_staged(s, EntityType.PRODUCT.value, p.id, f, caption="Imported from Excel")
                        photos += 1
                counts = plan.counts()
                audit.record(s, self.ctx, "EXCEL_IMPORT", "PRODUCT", None, plan.source.name,
                             f"Excel import of {plan.source.name} / {plan.options.sheet}: {created} created, "
                             f"{updated} updated, {counts['skip']} skipped, {counts['errors']} rows with errors, "
                             f"{photos} photo(s). Backup {backup.name}.",
                             new={"created": created, "updated": updated, "skipped": counts["skip"],
                                  "errors": counts["errors"], "photos": photos, "backup": backup.name,
                                  "suppliers_created": sorted(sup_new), "categories_created": sorted(cat_new)})
        except BaseException:
            att.discard(list(staged.values()))
            raise
        return ImportResult(created, updated, plan.counts()["skip"], plan.counts()["errors"], photos,
                            sorted(x.name for x in sup_new.values()), sorted(c.name for c in cat_new.values()),
                            backup, photo_problems)

    def _product_data(self, s, r: ImportRow, o: ImportOptions, suppliers, sup_new, categories, cat_new) -> dict:
        v = r.values
        mapped = set(o.mapping)
        if r.action == UPDATE:
            p = s.get(Product, r.existing_id)
            # Start from the product as it is; only columns that were mapped AND filled change it.
            data = {f: getattr(p, f) for f in ("item_type", "internal_code", "name", "supplier_code", "barcode",
                                                "description", "brand", "notes", "unit_id", "category_id",
                                                "supplier_id", "default_bin_id", "min_stock", "reorder_qty",
                                                "reorder_level", "unit_cost", "is_active")}
            if p.finished_info is not None:
                fi = p.finished_info
                data.update(production_date=fi.production_date, site_id=fi.site_id, customer=fi.customer,
                            status=fi.status)
        else:
            data = {"item_type": o.item_type, "internal_code": v["internal_code"], "unit_id": int(v["unit_id"]),
                    "is_active": True}
        for f in ("name", "description", "supplier_code", "barcode", "brand", "notes", *QTY_FIELDS, "unit_cost"):
            if f in mapped and v.get(f):
                data[f] = v[f]
            elif r.action == CREATE:
                data.setdefault(f, None)
        if v.get("default_bin_id"):
            data["default_bin_id"] = int(v["default_bin_id"])
        sup_text = (v.get("supplier") or "").strip()
        if sup_text:
            sup = suppliers.get(sup_text.upper())
            if sup is None:
                sup = self._new_supplier(s, sup_text, suppliers)
                sup_new[sup.name.upper()] = sup
            data["supplier_id"] = sup.id
        cat_text = (v.get("category") or "").strip()
        if cat_text:
            cat = categories.get(cat_text.upper())
            if cat is None:
                cat = Category(name=cat_text[:100], is_active=True)
                s.add(cat)
                s.flush()
                audit.record(s, self.ctx, "CATEGORY_SAVED", "CATEGORY", cat.id, cat.name, "Created by Excel import")
                categories[cat_text.upper()] = cat
                cat_new[cat_text.upper()] = cat
            data["category_id"] = cat.id
        return data

    def _new_supplier(self, s, name: str, suppliers: dict) -> Supplier:
        base = re.sub(r"[^A-Z0-9]", "", name.upper())[:10] or "SUP"
        code, n = base, 1
        while code in suppliers or s.scalar(select(Supplier.id).where(func.upper(Supplier.code) == code)):
            n += 1
            code = f"{base[:8]}{n}"
        sup = Supplier(code=code, name=name[:200], is_active=True)
        s.add(sup)
        s.flush()
        audit.record(s, self.ctx, "SUPPLIER_CREATED", "SUPPLIER", sup.id, name, "Created by Excel import")
        suppliers[name.upper()] = sup
        suppliers[code] = sup
        return sup

    # -- helpers --------------------------------------------------------------------------------
    @staticmethod
    def _check_file(path: str | Path) -> Path:
        p = Path(path)
        if not p.is_file():
            raise ValidationError(f"File not found: {p}")
        if p.suffix.lower() not in (".xlsx", ".xlsm"):
            raise ValidationError("Choose an Excel workbook (.xlsx). Older .xls files: open them in Excel and "
                                  "'Save As' .xlsx first.")
        return p

    @staticmethod
    def _load(path: Path):
        from openpyxl import load_workbook
        try:
            return load_workbook(path, data_only=True)
        except zipfile.BadZipFile:
            raise ValidationError(f"'{path.name}' is not a valid Excel workbook (or it is password protected).") \
                from None
        except PermissionError:
            raise ValidationError(f"'{path.name}' is open in another program. Close it and try again.") from None

    @staticmethod
    def _samples(ws, header_row: int, width: int, count: int = 5) -> list[list[str]]:
        """The first filled-in rows under the header, as text."""
        out = []
        for r in ws.iter_rows(min_row=header_row + 1, max_col=width, values_only=True):
            texts = [cell_text(v) for v in r]
            if any(texts):
                out.append(texts)
                if len(out) == count:
                    break
        return out

    @staticmethod
    def _headers(ws, header_row: int) -> list[str]:
        values = next(ws.iter_rows(min_row=header_row, max_row=header_row, values_only=True), ())
        last = max((i for i, v in enumerate(values) if v not in (None, "")), default=-1)
        width = max(last + 1, min(ws.max_column, 40))
        from openpyxl.utils import get_column_letter
        return [cell_text(values[i]) if i < len(values) and values[i] not in (None, "")
                else f"Column {get_column_letter(i + 1)}" for i in range(width)]


class PhotoFolder:
    """Finds a photo by its path or file name: as written, next to the workbook, or anywhere in the
    chosen picture folder (by file name, ignoring case)."""

    def __init__(self, folder: Path | None):
        self.folder = Path(folder) if folder else None
        self._by_name: dict[str, list[Path]] | None = None

    def _index(self) -> dict[str, list[Path]]:
        if self._by_name is None:
            self._by_name = {}
            if self.folder and self.folder.is_dir():
                for f in self.folder.rglob("*"):
                    if f.is_file() and f.suffix.lower() in PHOTO_EXT:
                        self._by_name.setdefault(f.name.lower(), []).append(f)
        return self._by_name

    def find(self, text: str, workbook_dir: Path) -> Path | None:
        t = text.strip()
        if t.lower().startswith("file:"):
            from urllib.parse import unquote, urlparse
            t = unquote(urlparse(t).path)
            if re.match(r"^/[A-Za-z]:", t):
                t = t[1:]
        candidate = Path(t.replace("\\", "/")) if "\\" in t and not Path(t).exists() else Path(t)
        for p in (candidate, workbook_dir / candidate):
            if p.is_file():
                return p
        name = Path(t.replace("\\", "/")).name.lower()
        hits = self._index().get(name, [])
        if len(hits) == 1:
            return hits[0]
        if not hits and self.folder is not None and (self.folder / name).is_file():
            return self.folder / name
        return hits[0] if hits else None


def copy_template(dest: Path) -> Path:
    """A blank workbook with the columns the importer understands (for people starting from scratch)."""
    from openpyxl import Workbook
    from openpyxl.styles import Font
    wb = Workbook()
    ws = wb.active
    ws.title = "Products"
    for i, f in enumerate(FIELDS, start=1):
        c = ws.cell(row=1, column=i, value=FIELDS[f][0].rstrip(" *"))
        c.font = Font(bold=True)
    ws.freeze_panes = "A2"
    wb.save(dest)
    return dest


__all__ = ["ACTION_LABELS", "CREATE", "FIELDS", "SKIP", "UPDATE", "ExcelImportService", "ImportOptions",
           "ImportPlan", "ImportResult", "ImportRow", "SheetInfo", "WorkbookInfo", "copy_template", "guess_mapping"]
