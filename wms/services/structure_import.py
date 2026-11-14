"""Excel import of a project's structure: Project -> [Block -> Level ->] Unit (+ unit materials) (v1.25).

The person fills in the template (``write_template``): one row per unit on the *Structure* sheet and,
optionally, one row per material of a unit on the *Materials* sheet. The workflow is

    choose the workbook -> preview (every row checked, nothing written) -> Import

and the import itself

* takes a safety backup first and stops if that fails;
* re-checks the workbook against the database and refuses if anything changed since the preview;
* writes everything in ONE transaction - all or nothing;
* never deletes or moves anything: rows add what is missing and update what differs
  (unit quantity, unit code, notes, quantity per unit, VPK code). Units, blocks and levels that are
  in the WMS but not in the workbook are left exactly as they are;
* never changes stock. Unit materials only feed the project's Required list (as the tree does), so
  Remaining, the Production dashboard and every report pick the imported units up at once.

Matching (case, spaces and punctuation ignored):

* project  - by Project No; a new number makes a new project (status Planned), which needs a name
             that no other project has;
* block    - by name inside the project, level by name inside the block;
* unit     - by Unit Code inside its place when a code is given, otherwise by Unit Name;
* material - by the material's Code in the WMS (materials are never created here: import them on
             the Materials screen first).
"""

from __future__ import annotations

import re
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from sqlalchemy import func, select

from wms.core.enums import EntityType, SiteStatus
from wms.core.exceptions import DuplicateError, ValidationError, WMSError
from wms.core.numbers import ZERO, to_qty
from wms.core.permissions import P
from wms.db.models import Manager, Product, ProjectBlock, ProjectLevel, ProjectUnit, Site, UnitMaterial
from wms.services import audit
from wms.services.base import BaseService
from wms.services.products import code_key, code_key_sql
from wms.services.unit_norms import NORM_ALL, highest_marked_piece, record_norm, units_done

# ---- statuses ---------------------------------------------------------------------------------
NEW, UPDATE, SAME, ERROR = "New", "Update", "No change", "Error"

STRUCTURE_SHEET, MATERIALS_SHEET, HELP_SHEET, EXAMPLE_SHEET = "Structure", "Materials", "How to fill in", "Example"

#: field -> (template header, other header words that mean the same)
STRUCTURE_FIELDS: dict[str, tuple[str, tuple[str, ...]]] = {
    "manager": ("Project Manager", ("project manager", "manager", "pm", "менеджер")),
    "project_name": ("Project Name", ("project name", "name of project", "назва проекту")),
    "project_no": ("Project No *", ("project no", "project number", "project", "job no", "job number", "job",
                                    "номер проекту", "проект", "проєкт")),
    "block": ("Block", ("block", "building", "блок", "корпус")),
    "level": ("Level", ("level", "floor", "storey", "поверх", "рівень")),
    "unit_code": ("Unit Code", ("unit code", "unit no", "unit number", "unit ref", "код юніта", "код")),
    "unit_name": ("Unit Name *", ("unit name", "unit", "unit type", "name", "юніт", "назва")),
    "quantity": ("Quantity *", ("quantity", "qty", "units", "кількість", "к-сть")),
    "notes": ("Notes", ("notes", "note", "comment", "comments", "примітки")),
}
MATERIAL_FIELDS: dict[str, tuple[str, tuple[str, ...]]] = {
    "project_no": STRUCTURE_FIELDS["project_no"],
    "block": STRUCTURE_FIELDS["block"],
    "level": STRUCTURE_FIELDS["level"],
    "unit_code": STRUCTURE_FIELDS["unit_code"],
    "unit_name": ("Unit Name", ("unit name", "unit", "unit type", "юніт")),
    "material_code": ("Material Code *", ("material code", "material", "code", "internal code", "product code",
                                          "item code", "код матеріалу", "матеріал")),
    "qty_per_unit": ("Qty per Unit *", ("qty per unit", "quantity per unit", "per unit", "norm", "qty",
                                        "quantity", "норма", "кількість на юніт")),
    "vpk_code": ("VPK Code", ("vpk code", "vpk", "впк")),
    "notes": STRUCTURE_FIELDS["notes"],
}

STRUCTURE_HELP = (
    ("Project Manager", "Optional, for a new project. A new name creates the manager."),
    ("Project Name", "Needed only for a NEW project (on at least one of its rows). For a project already in the "
                     "WMS the name is not changed."),
    ("Project No *", "The project number, e.g. P-1042. A number already in the WMS adds to that project; a new "
                     "number creates a new project (status Planned)."),
    ("Block", "Optional (e.g. Block A). Leave Block and Level empty for a unit that sits directly in the project."),
    ("Level", "Optional (e.g. Level 3). If Block is filled, Level must be filled too (units sit on a level)."),
    ("Unit Code", "Optional (e.g. U-01). When filled, the unit is matched by its code; otherwise by its name."),
    ("Unit Name *", "The unit / unit type, e.g. Kitchen Type A. One row per unit."),
    ("Quantity *", "How many of this unit (0 or more, whole or decimal)."),
    ("Notes", "Optional."),
)
MATERIAL_HELP = (
    ("Project No, Block, Level", "Where the unit is - exactly as on the Structure sheet."),
    ("Unit Code / Unit Name", "Which unit the material is for (the code, or the name when the unit has no code)."),
    ("Material Code *", "The material's Code in the WMS. The material must already exist (Materials screen)."),
    ("Qty per Unit *", "How much of the material ONE unit needs (the norm), in the material's unit (sheets, m, pcs)."),
    ("VPK Code", "Optional customer / designer reference for this line."),
    ("Notes", "Optional."),
)
RULES = (
    "Nothing is deleted: units, blocks and levels in the WMS that are not in the workbook stay as they are.",
    "Importing the same workbook again changes nothing - only differences are written.",
    "The preview shows every row (New / Update / No change / Error). The import starts only when there are no errors.",
    "A safety backup is taken before the import, and the import is all or nothing.",
    "The Materials sheet is optional. Stock is never changed by this import.",
)
EXAMPLE_STRUCTURE = (
    ("John Smith", "Riverside Apartments", "P-1042", "Block A", "Level 1", "U-01", "Kitchen Type A", 12, ""),
    ("John Smith", "Riverside Apartments", "P-1042", "Block A", "Level 1", "U-02", "Wardrobe 2-door", 24, ""),
    ("John Smith", "Riverside Apartments", "P-1042", "Block A", "Level 2", "U-01", "Kitchen Type A", 10, ""),
    ("John Smith", "Riverside Apartments", "P-1042", "Block B", "Level 1", "U-01", "Kitchen Type B", 8, "Show flat on Level 1"),
    ("Anna Brown", "Mill Lane Houses", "P-1050", "", "", "", "Kitchen Standard", 30, "No blocks or levels"),
)
EXAMPLE_MATERIALS = (
    ("P-1042", "Block A", "Level 1", "U-01", "Kitchen Type A", "MFC-W980-18", 2.5, "VPK-11", ""),
    ("P-1042", "Block A", "Level 1", "U-01", "Kitchen Type A", "EDGE-W980-22", 34, "", "metres"),
    ("P-1050", "", "", "", "Kitchen Standard", "MFC-W980-18", 3, "", ""),
)


def _key(text: str | None) -> str:
    """Names as compared: case, spaces and punctuation ignored ("Block-A" = "block a")."""
    return re.sub(r"[\W_]+", " ", (text or "").casefold()).strip()


def _text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return " ".join(str(value).split())


def _fmt(q: Decimal | None) -> str:
    return "" if q is None else f"{q.normalize():f}"


# ---- what the preview shows ----------------------------------------------------------------------
@dataclass
class Row:
    sheet: str
    row: int
    values: dict[str, str]
    status: str = NEW
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    changes: list[str] = field(default_factory=list)
    #: filled in by the check: the place and the unit / line the row means
    place: tuple[str, str, str] = ("", "", "")
    unit_key: tuple | None = None
    existing_id: int | None = None
    product_id: int | None = None
    qty: Decimal | None = None

    def get(self, name: str) -> str:
        return self.values.get(name, "")

    def error(self, text: str) -> None:
        self.errors.append(text)
        self.status = ERROR

    @property
    def where(self) -> str:
        p, b, lv = (self.get("project_no"), self.get("block"), self.get("level"))
        return " / ".join(x for x in (p, b, lv) if x)

    @property
    def problems(self) -> str:
        return "; ".join(self.errors + self.warnings)


@dataclass
class ProjectPlan:
    number: str
    name: str
    manager: str
    site_id: int | None
    new_blocks: int = 0
    new_levels: int = 0

    @property
    def is_new(self) -> bool:
        return self.site_id is None


@dataclass
class StructurePlan:
    source: Path
    units: list[Row]
    materials: list[Row]
    projects: dict[str, ProjectPlan]
    problems: list[str] = field(default_factory=list)  # workbook-level (missing sheet / columns)

    @property
    def rows(self) -> list[Row]:
        return self.units + self.materials

    @property
    def errors(self) -> int:
        return sum(1 for r in self.rows if r.status == ERROR) + len(self.problems)

    @property
    def can_import(self) -> bool:
        return self.errors == 0 and self.changes > 0

    @property
    def changes(self) -> int:
        return sum(1 for r in self.rows if r.status in (NEW, UPDATE)) + sum(1 for p in self.projects.values()
                                                                           if p.is_new)

    def count(self, rows: list[Row], status: str) -> int:
        return sum(1 for r in rows if r.status == status)

    def fingerprint(self) -> tuple:
        """What the import would do - compared again just before importing."""
        return tuple((r.sheet, r.row, r.status, tuple(r.changes)) for r in self.rows) + tuple(
            (p.number, p.is_new, p.new_blocks, p.new_levels) for p in self.projects.values())

    def summary(self) -> str:
        new_p = [p for p in self.projects.values() if p.is_new]
        blocks = sum(p.new_blocks for p in self.projects.values())
        levels = sum(p.new_levels for p in self.projects.values())
        parts = [f"Projects: {len(self.projects)} ({len(new_p)} new)",
                 f"new blocks: {blocks}, new levels: {levels}",
                 f"Units: {self.count(self.units, NEW)} new, {self.count(self.units, UPDATE)} to update, "
                 f"{self.count(self.units, SAME)} unchanged"]
        if self.materials:
            parts.append(f"Unit materials: {self.count(self.materials, NEW)} new, "
                         f"{self.count(self.materials, UPDATE)} to update, {self.count(self.materials, SAME)} unchanged")
        if self.errors:
            parts.append(f"ERRORS: {self.errors}")
        return " · ".join(parts)


@dataclass
class StructureResult:
    projects_created: int = 0
    blocks_created: int = 0
    levels_created: int = 0
    units_created: int = 0
    units_updated: int = 0
    lines_created: int = 0
    lines_updated: int = 0
    backup: Path | None = None
    site_ids: list[int] = field(default_factory=list)

    def summary(self) -> str:
        return (f"Projects created: {self.projects_created}; blocks: {self.blocks_created}; levels: "
                f"{self.levels_created}; units created: {self.units_created}, updated: {self.units_updated}; "
                f"unit materials added: {self.lines_created}, updated: {self.lines_updated}.")


# ---- the existing structure, indexed by the same keys as the workbook ------------------------------
@dataclass
class _Known:
    sites: dict[str, Site] = field(default_factory=dict)                      # number key -> site
    blocks: dict[tuple[str, str], int] = field(default_factory=dict)          # (p, b) -> block id
    levels: dict[tuple[str, str, str], int] = field(default_factory=dict)     # (p, b, l) -> level id
    by_code: dict[tuple, ProjectUnit] = field(default_factory=dict)           # (place, code key) -> unit
    by_name: dict[tuple, list[ProjectUnit]] = field(default_factory=dict)     # (place, name key) -> units
    lines: dict[tuple[int, int], UnitMaterial] = field(default_factory=dict)  # (unit id, product id) -> line
    names: dict[str, Site] = field(default_factory=dict)                      # name key -> site (any project)


def _number_key(number: str) -> str:
    return " ".join(number.split()).upper()


# ---- the service --------------------------------------------------------------------------------
class StructureImportService(BaseService):

    def prepare(self, path: str | Path) -> StructurePlan:
        """Read the workbook and check every row against the database. Writes nothing."""
        self.require(P.SITES_EDIT)
        p = _check_file(path)
        wb = _load(p)
        try:
            problems: list[str] = []
            ws_units = _find_sheet(wb, STRUCTURE_SHEET, STRUCTURE_FIELDS, "unit_name")
            ws_mat = _find_sheet(wb, MATERIALS_SHEET, MATERIAL_FIELDS, "material_code", exclude=ws_units)
            units: list[Row] = []
            materials: list[Row] = []
            if ws_units is None:
                problems.append(f"No '{STRUCTURE_SHEET}' sheet with the columns Project No, Unit Name and Quantity "
                                f"was found. Use the template (Projects > Import structure > Template).")
            else:
                units = _read(ws_units, STRUCTURE_FIELDS, ("project_no", "unit_name", "quantity"), problems)
            if ws_mat is not None:
                materials = _read(ws_mat, MATERIAL_FIELDS, ("project_no", "material_code", "qty_per_unit"), problems)
        finally:
            wb.close()
        plan = StructurePlan(p, units, materials, {}, problems)
        if not problems and not units and not materials:
            problems.append("The workbook has no filled-in rows.")
        if not problems:
            with self.db.read_session() as s:
                known = self._known(s, plan)
                self._check_units(s, plan, known)
                self._check_materials(s, plan, known)
        return plan

    # -- reading the database ----------------------------------------------------------------------
    def _known(self, s, plan: StructurePlan) -> _Known:
        k = _Known()
        numbers = {_number_key(r.get("project_no")) for r in plan.rows if r.get("project_no")}
        if numbers:
            for site in s.scalars(select(Site).where(func.upper(Site.project_number).in_(list(numbers)))):
                k.sites[_number_key(site.project_number)] = site
        from wms.services.sites import project_name_key
        keys = sorted({project_name_key(r.get("project_name")) for r in plan.units if r.get("project_name")} - {None})
        if keys:
            for site in s.scalars(select(Site).where(Site.name_key.in_(keys)).order_by(Site.id)):
                k.names.setdefault(site.name_key, site)
        site_ids = {site.id: nk for nk, site in k.sites.items()}
        if not site_ids:
            return k
        block_key: dict[int, tuple[str, str]] = {}
        for b in s.scalars(select(ProjectBlock).where(ProjectBlock.site_id.in_(list(site_ids)))
                           .order_by(ProjectBlock.sort_order, ProjectBlock.id)):
            key = (site_ids[b.site_id], _key(b.name))
            k.blocks.setdefault(key, b.id)
            block_key[b.id] = key
        level_place: dict[int, tuple[str, str, str]] = {}
        if block_key:
            for lv in s.scalars(select(ProjectLevel).where(ProjectLevel.block_id.in_(list(block_key)))
                                .order_by(ProjectLevel.sort_order, ProjectLevel.id)):
                bk = block_key[lv.block_id]
                key = (bk[0], bk[1], _key(lv.name))
                k.levels.setdefault(key, lv.id)
                level_place[lv.id] = key
        unit_ids = []
        for u in s.scalars(select(ProjectUnit).where(ProjectUnit.site_id.in_(list(site_ids)))
                           .order_by(ProjectUnit.sort_order, ProjectUnit.id)):
            place = level_place.get(u.level_id) if u.level_id else (site_ids[u.site_id], "", "")
            if place is None:
                continue
            if u.unit_code:
                k.by_code.setdefault((place, code_key(u.unit_code)), u)
            k.by_name.setdefault((place, _key(u.name)), []).append(u)
            unit_ids.append(u.id)
        for i in range(0, len(unit_ids), 900):
            for line in s.scalars(select(UnitMaterial).where(UnitMaterial.unit_id.in_(unit_ids[i:i + 900]))):
                k.lines.setdefault((line.unit_id, line.product_id), line)
        return k

    # -- the Structure sheet -----------------------------------------------------------------------
    def _check_units(self, s, plan: StructurePlan, k: _Known) -> None:
        seen: dict[tuple, Row] = {}
        new_blocks: set[tuple[str, str]] = set()
        new_levels: set[tuple[str, str, str]] = set()
        new_names: dict[str, str] = {}  # name key -> project number (new projects in the file)
        for r in plan.units:
            number = r.get("project_no")
            if not number:
                r.error("Project No is empty.")
            if len(number) > 50:
                r.error("Project No is longer than 50 characters.")
            if not r.get("unit_name"):
                r.error("Unit Name is empty.")
            for f, n in (("unit_name", 200), ("block", 200), ("level", 200), ("unit_code", 50),
                         ("project_name", 200), ("manager", 120)):
                if len(r.get(f)) > n:
                    r.error(f"{STRUCTURE_FIELDS[f][0].rstrip(' *')} is longer than {n} characters.")
            r.qty = _qty(r, "quantity", "Quantity")
            if r.get("level") and not r.get("block"):
                r.error("Level is filled but Block is empty: fill in the Block of this level.")
            if r.get("block") and not r.get("level"):
                r.error("Block is filled but Level is empty: units sit on a level of a block - fill in the Level, "
                        "or leave both empty for a unit directly in the project.")
            if not number:
                continue
            nk = _number_key(number)
            site = k.sites.get(nk)
            proj = plan.projects.get(nk)
            if proj is None:
                proj = plan.projects[nk] = ProjectPlan(number, "", "", site.id if site else None)
            self._check_project(r, proj, site, k, new_names)
            r.place = (nk, _key(r.get("block")), _key(r.get("level")))
            if r.get("unit_name"):
                r.unit_key = _unit_key(r.place, r.get("unit_code"), r.get("unit_name"))
                first = seen.get(r.unit_key)
                if first is not None:
                    r.error(f"The same unit is already on row {first.row} ({first.where} / "
                            f"{r.get('unit_code') or r.get('unit_name')}). One row per unit.")
                else:
                    seen[r.unit_key] = r
            if r.status == ERROR:
                continue
            if r.place[1]:
                if (nk, r.place[1]) not in k.blocks and (nk, r.place[1]) not in new_blocks:
                    new_blocks.add((nk, r.place[1]))
                    proj.new_blocks += 1
                if r.place not in k.levels and r.place not in new_levels:
                    new_levels.add(r.place)
                    proj.new_levels += 1
            self._match_unit(s, r, k)
        for nk, proj in plan.projects.items():  # a new project needs a name on one of its rows
            if proj.is_new and not proj.name:
                first = next(r for r in plan.units if r.place[0] == nk or _number_key(r.get("project_no")) == nk)
                if not any("same name" in e or "already exists" in e for e in first.errors):
                    first.error(f"Project {proj.number} is not in the WMS: fill in its Project Name to create it "
                                f"(or check the number).")

    def _check_project(self, r: Row, proj: ProjectPlan, site: Site | None, k: _Known, new_names: dict) -> None:
        name, manager = r.get("project_name"), r.get("manager")
        if site is not None:
            if site.status == SiteStatus.ARCHIVED.value:
                r.error(f"Project {site.project_number} is archived: restore it before importing units into it.")
            if name and _key(name) != _key(site.name):
                r.warnings.append(f"Project name in the WMS is '{site.name}' - it is not changed by the import.")
            if manager and _key(manager) != _key(site.project_manager):
                r.warnings.append(f"Project manager in the WMS is '{site.project_manager or '(none)'}' - it is not "
                                  f"changed by the import.")
            return
        if name and not proj.name:
            from wms.services.sites import project_name_key
            nkey = project_name_key(name)
            other = k.names.get(nkey)
            if other is not None:
                r.error(f"A project named '{other.name}' already exists ({other.project_number}). Use its number "
                        f"{other.project_number}, or choose another name.")
                return
            if nkey in new_names and new_names[nkey] != proj.number:
                r.error(f"Project {new_names[nkey]} in this workbook has the same name. Project names must differ.")
                return
            new_names[nkey] = proj.number
            proj.name = name
        elif name and _key(name) != _key(proj.name):
            r.warnings.append(f"Another row names this project '{proj.name}' - that name is used.")
        if manager and not proj.manager:
            proj.manager = manager

    def _match_unit(self, s, r: Row, k: _Known) -> None:
        u = _find_unit(k, r.place, r.get("unit_code"), r.get("unit_name"))
        if u is None:
            r.status = NEW
            r.changes.append(f"New unit x {_fmt(r.qty)}")
            return
        r.existing_id = u.id
        if r.qty is not None and r.qty != u.quantity:
            done = units_done(s, u.id)
            top = highest_marked_piece(s, u.id)
            if r.qty < done:
                r.error(f"{_fmt(done)} unit(s) of {u.name} are already done: the quantity cannot be less than that.")
            elif r.qty < top:
                r.error(f"Piece #{top} of {u.name} has marks on the Production dashboard: the quantity cannot be "
                        f"less than {top}.")
            else:
                r.changes.append(f"Quantity {_fmt(u.quantity)} -> {_fmt(r.qty)}")
        code = r.get("unit_code")
        if code and code != (u.unit_code or ""):
            r.changes.append(f"Unit code '{u.unit_code or ''}' -> '{code}'")
        if r.get("unit_name") != u.name and r.get("unit_code") and _key(r.get("unit_name")) != _key(u.name):
            r.changes.append(f"Name '{u.name}' -> '{r.get('unit_name')}'")
        if r.get("notes") and r.get("notes") != (u.notes or ""):
            r.changes.append("Notes")
        if r.status != ERROR:
            r.status = UPDATE if r.changes else SAME

    # -- the Materials sheet -----------------------------------------------------------------------
    def _check_materials(self, s, plan: StructurePlan, k: _Known) -> None:
        file_units: dict[tuple, Row] = {}
        for r in plan.units:  # a unit of the Structure sheet: by its code, and by its name
            if r.unit_key:
                file_units.setdefault(r.unit_key, r)
                file_units.setdefault((r.place, "N", _key(r.get("unit_name"))), r)
        codes = {code_key(r.get("material_code")) for r in plan.materials if r.get("material_code")}
        products: dict[str, Product] = {}
        keys = [c for c in codes if c]
        for i in range(0, len(keys), 900):
            for prod in s.scalars(select(Product).where(code_key_sql(Product.internal_code).in_(keys[i:i + 900]))
                                  .order_by(Product.id)):
                products.setdefault(code_key(prod.internal_code), prod)
        seen: dict[tuple, Row] = {}
        for r in plan.materials:
            number = r.get("project_no")
            if not number:
                r.error("Project No is empty.")
            if not r.get("unit_code") and not r.get("unit_name"):
                r.error("Fill in the Unit Code or the Unit Name of the unit this material is for.")
            if not r.get("material_code"):
                r.error("Material Code is empty.")
            if len(r.get("vpk_code")) > 80:
                r.error("VPK Code is longer than 80 characters.")
            r.qty = _qty(r, "qty_per_unit", "Qty per Unit")
            if r.status == ERROR:
                continue
            nk = _number_key(number)
            r.place = (nk, _key(r.get("block")), _key(r.get("level")))
            prod = products.get(code_key(r.get("material_code")))
            if prod is None:
                r.error(f"Material '{r.get('material_code')}' is not in the WMS. Add it on the Materials screen "
                        f"(or import the materials list) first.")
            elif not prod.is_active:
                r.warnings.append(f"Material {prod.internal_code} is inactive.")
            # the unit: a row of the Structure sheet, or a unit already in the WMS
            unit_row = None
            for key in _candidate_keys(r.place, r.get("unit_code"), r.get("unit_name")):
                unit_row = file_units.get(key)
                if unit_row is not None:
                    break
            existing = None
            if unit_row is not None:
                if unit_row.status == ERROR:
                    r.error(f"Its unit (Structure row {unit_row.row}) has errors.")
                existing = unit_row.existing_id
                r.unit_key = unit_row.unit_key
            else:
                u = _find_unit(k, r.place, r.get("unit_code"), r.get("unit_name"))
                if u is None:
                    r.error(f"Unit '{r.get('unit_code') or r.get('unit_name')}' in {r.where} is neither on the "
                            f"Structure sheet nor in the WMS.")
                else:
                    if k.sites[nk].status == SiteStatus.ARCHIVED.value:
                        r.error(f"Project {k.sites[nk].project_number} is archived.")
                    existing = u.id
                    r.unit_key = ("ID", u.id)
            if r.status == ERROR or prod is None:
                continue
            r.product_id = prod.id
            first = seen.get((r.unit_key, prod.id))
            if first is not None:
                r.error(f"Material {prod.internal_code} for this unit is already on row {first.row}.")
                continue
            seen[(r.unit_key, prod.id)] = r
            line = k.lines.get((existing, prod.id)) if existing else None
            if line is None:
                r.status = NEW
                r.changes.append(f"{prod.internal_code} x {_fmt(r.qty)} per unit")
                continue
            r.existing_id = line.id
            if r.qty != line.quantity_per_unit:
                r.changes.append(f"Per unit {_fmt(line.quantity_per_unit)} -> {_fmt(r.qty)}")
                if units_done(s, line.unit_id) > ZERO:
                    r.warnings.append("Some units are already done: they are recalculated with the new norm.")
            if r.get("vpk_code") and r.get("vpk_code") != (line.vpk_code or ""):
                r.changes.append(f"VPK '{line.vpk_code or ''}' -> '{r.get('vpk_code')}'")
            if r.get("notes") and r.get("notes") != (line.notes or ""):
                r.changes.append("Notes")
            r.status = UPDATE if r.changes else SAME

    # -- the import --------------------------------------------------------------------------------
    def run(self, plan: StructurePlan, progress: Callable[[int, int], bool] | None = None) -> StructureResult:
        """Import the previewed workbook: backup, re-check, then one transaction."""
        self.require(P.SITES_EDIT)
        fresh = self.prepare(plan.source)
        if fresh.errors:
            raise ValidationError("The workbook has errors - check the preview again.")
        if fresh.fingerprint() != plan.fingerprint():
            raise ValidationError("The workbook or the projects changed since the preview. Press Check again and "
                                  "import after looking at the new preview.")
        if not fresh.changes:
            raise ValidationError("Nothing to import: the WMS already matches the workbook.")
        from wms.services.backup import BackupService
        try:
            backup = BackupService(self.ctx).create_safety(
                f"Pre-import backup before project structure import of {plan.source.name}")
        except WMSError:
            raise
        except Exception as exc:  # noqa: BLE001 - no backup, no import
            raise ValidationError(f"The pre-import backup failed, so nothing was imported: {exc}") from exc
        res = StructureResult(backup=backup)
        with self.db.session() as s:
            _Writer(self, s, fresh, res, progress).write()
        return res


class _Writer:
    """Writes a checked plan inside one session (the caller's transaction)."""

    def __init__(self, svc: StructureImportService, s, plan: StructurePlan, res: StructureResult, progress):
        self.svc, self.s, self.plan, self.res, self.progress = svc, s, plan, res, progress
        self.ctx = svc.ctx
        self.user_id = self.ctx.user.id if self.ctx.user else None
        self.sites: dict[str, Site] = {}
        self.blocks: dict[tuple[str, str], int] = {}
        self.levels: dict[tuple[str, str, str], int] = {}
        self.units: dict[tuple, ProjectUnit] = {}
        self.order: dict[tuple, int] = {}
        self.changed_sites: set[int] = set()

    def write(self) -> None:
        from wms.services.project_demand import sync_project_lists
        s, plan = self.s, self.plan
        total, done = len(plan.rows), 0
        for nk, proj in plan.projects.items():
            self.sites[nk] = self._project(proj)
        known = self.svc._known(s, plan)
        self.blocks, self.levels = dict(known.blocks), dict(known.levels)
        for r in plan.units:
            self._unit(r, known)
            done += 1
            self._tick(done, total)
        for r in plan.materials:
            self._line(r)
            done += 1
            self._tick(done, total)
        self.res.site_ids = sorted(self.changed_sites)
        sync_project_lists(s, self.ctx, self.res.site_ids)
        for site_id in self.res.site_ids:
            site = s.get(Site, site_id)
            audit.record(s, self.ctx, "PROJECT_STRUCTURE_IMPORTED", EntityType.SITE.value, site.id, site.display,
                             f"Imported from {plan.source.name}: {self.res.summary()}",
                             new={"file": plan.source.name})

    def _tick(self, done: int, total: int) -> None:
        if self.progress is not None and self.progress(done, total) is False:
            raise ValidationError("Import cancelled - nothing was imported (the WMS is exactly as before).")

    def _project(self, proj: ProjectPlan) -> Site:
        s = self.s
        if proj.site_id is not None:
            return s.get(Site, proj.site_id)
        from wms.services.project_tree import find_or_create_manager
        from wms.services.sites import check_project_name
        if not proj.name:
            raise ValidationError(f"Project {proj.number} is new: fill in its Project Name.")
        try:
            name_key = check_project_name(s, proj.name)
        except DuplicateError as exc:
            raise ValidationError(str(exc)) from None
        site = Site(project_number=proj.number, name=proj.name, name_key=name_key, status=SiteStatus.PLANNED.value)
        m: Manager | None = find_or_create_manager(s, self.ctx, proj.manager) if proj.manager else None
        if m is not None:
            site.manager_id, site.project_manager = m.id, m.name
        s.add(site)
        s.flush()
        audit.record(s, self.ctx, "SITE_CREATED", "SITE", site.id, site.display,
                     f"Created by the project structure import ({self.plan.source.name})",
                     new={"project_number": site.project_number, "name": site.name, "status": site.status})
        self.res.projects_created += 1
        self.changed_sites.add(site.id)
        return site

    def _level_id(self, r: Row) -> int | None:
        nk, bk, lk = r.place
        if not bk:
            return None
        site = self.sites[nk]
        s = self.s
        if (nk, bk) not in self.blocks:
            order = (s.scalar(select(func.max(ProjectBlock.sort_order)).where(ProjectBlock.site_id == site.id)) or 0) + 1
            b = ProjectBlock(site_id=site.id, name=r.get("block"), sort_order=order, created_by_id=self.user_id)
            s.add(b)
            s.flush()
            self.blocks[(nk, bk)] = b.id
            self.res.blocks_created += 1
            self.changed_sites.add(site.id)
            audit.record(s, self.ctx, "BLOCK_CREATED", EntityType.SITE.value, site.id,
                         f"{site.project_number} / {b.name}", new={"name": b.name})
        if r.place not in self.levels:
            block_id = self.blocks[(nk, bk)]
            order = (s.scalar(select(func.max(ProjectLevel.sort_order)).where(ProjectLevel.block_id == block_id))
                     or 0) + 1
            lv = ProjectLevel(block_id=block_id, name=r.get("level"), sort_order=order, created_by_id=self.user_id)
            s.add(lv)
            s.flush()
            self.levels[r.place] = lv.id
            self.res.levels_created += 1
            self.changed_sites.add(site.id)
            audit.record(s, self.ctx, "LEVEL_CREATED", EntityType.SITE.value, site.id,
                         f"{site.project_number} / {r.get('block')} / {lv.name}", new={"name": lv.name})
        return self.levels[r.place]

    def _unit(self, r: Row, known: _Known) -> None:
        s = self.s
        site = self.sites[r.place[0]]
        if r.status == SAME:
            self.units[r.unit_key] = s.get(ProjectUnit, r.existing_id)
            return
        if r.status == UPDATE:
            u = s.get(ProjectUnit, r.existing_id)
            old = audit.snapshot(u, ("unit_code", "name", "quantity", "notes"))
            if r.qty is not None:
                u.quantity = r.qty
            if r.get("unit_code"):
                u.unit_code = r.get("unit_code")
                u.name = r.get("unit_name")
            if r.get("notes"):
                u.notes = r.get("notes")
            s.flush()
            o, n = audit.diff(old, audit.snapshot(u, ("unit_code", "name", "quantity", "notes")))
            if n:
                audit.record(s, self.ctx, "UNIT_UPDATED", EntityType.PROJECT_UNIT.value, u.id,
                             f"{site.project_number} / {u.name}", "Project structure import", old=o, new=n)
            self.units[r.unit_key] = u
            self.res.units_updated += 1
            self.changed_sites.add(site.id)
            return
        level_id = self._level_id(r)
        sib = (site.id, level_id)
        if sib not in self.order:
            where = [ProjectUnit.level_id == level_id] if level_id else [ProjectUnit.site_id == site.id,
                                                                         ProjectUnit.level_id.is_(None)]
            self.order[sib] = s.scalar(select(func.max(ProjectUnit.sort_order)).where(*where)) or 0
        self.order[sib] += 1
        u = ProjectUnit(site_id=site.id, level_id=level_id, unit_code=r.get("unit_code") or None,
                        name=r.get("unit_name"), quantity=r.qty, notes=r.get("notes") or None,
                        sort_order=self.order[sib], created_by_id=self.user_id)
        s.add(u)
        s.flush()
        audit.record(s, self.ctx, "UNIT_CREATED", EntityType.PROJECT_UNIT.value, u.id,
                     f"{site.project_number} / {u.name}", "Project structure import",
                     new={"name": u.name, "unit_code": u.unit_code, "quantity": str(u.quantity)})
        self.units[r.unit_key] = u
        self.res.units_created += 1
        self.changed_sites.add(site.id)

    def _line(self, r: Row) -> None:
        if r.status == SAME:
            return
        s = self.s
        if r.status == UPDATE:
            line = s.get(UnitMaterial, r.existing_id)
            old_norm = line.quantity_per_unit
            old = audit.snapshot(line, ("quantity_per_unit", "vpk_code", "notes"))
            line.quantity_per_unit = r.qty
            if r.get("vpk_code"):
                line.vpk_code = r.get("vpk_code")
            if r.get("notes"):
                line.notes = r.get("notes")
            s.flush()
            o, n = audit.diff(old, audit.snapshot(line, ("quantity_per_unit", "vpk_code", "notes")))
            u = s.get(ProjectUnit, line.unit_id)
            if n:
                audit.record(s, self.ctx, "UNIT_MATERIAL_UPDATED", EntityType.PROJECT_UNIT.value, u.id,
                             f"{u.site.project_number} / {u.name}", line.product.internal_code, old=o, new=n)
                if "quantity_per_unit" in n:
                    record_norm(s, self.ctx, line, old_norm, NORM_ALL, "Project structure import")
            self.res.lines_updated += 1
            self.changed_sites.add(u.site_id)
            return
        u = (s.get(ProjectUnit, r.unit_key[1]) if r.unit_key and r.unit_key[0] == "ID"
             else self.units[r.unit_key])
        order = (s.scalar(select(func.max(UnitMaterial.sort_order)).where(UnitMaterial.unit_id == u.id)) or 0) + 1
        line = UnitMaterial(unit_id=u.id, product_id=r.product_id, quantity_per_unit=r.qty,
                            vpk_code=r.get("vpk_code") or None, notes=r.get("notes") or None, sort_order=order,
                            created_by_id=self.user_id)
        s.add(line)
        s.flush()
        record_norm(s, self.ctx, line, None, NORM_ALL, "Project structure import")
        prod = s.get(Product, r.product_id)
        audit.record(s, self.ctx, "UNIT_MATERIAL_ADDED", EntityType.PROJECT_UNIT.value, u.id,
                     f"{u.site.project_number} / {u.name}", f"{prod.internal_code} x {line.quantity_per_unit} per unit",
                     new={"product": prod.internal_code, "quantity_per_unit": str(line.quantity_per_unit)})
        self.res.lines_created += 1
        self.changed_sites.add(u.site_id)


# ---- helpers ------------------------------------------------------------------------------------
def _unit_key(place: tuple, code: str, name: str) -> tuple:
    return (place, "C", code_key(code)) if code else (place, "N", _key(name))


def _candidate_keys(place: tuple, code: str, name: str) -> list[tuple]:
    keys = []
    if code:
        keys.append((place, "C", code_key(code)))
    if name:
        keys.append((place, "N", _key(name)))
    return keys


def _find_unit(k: _Known, place: tuple, code: str, name: str) -> ProjectUnit | None:
    """A unit already in the WMS: by code when a code is given (or a unit of that name without a
    code), otherwise by name."""
    if code:
        u = k.by_code.get((place, code_key(code)))
        if u is not None:
            return u
        return next((u for u in k.by_name.get((place, _key(name)), []) if not u.unit_code), None) if name else None
    units = k.by_name.get((place, _key(name)), [])
    return units[0] if units else None


def _qty(r: Row, f: str, label: str) -> Decimal | None:
    text = r.get(f)
    if not text:
        r.error(f"{label} is empty.")
        return None
    try:
        q = to_qty(text.replace(",", "."), label.lower())
    except (ValidationError, ValueError, ArithmeticError):
        r.error(f"{label} '{text}' is not a number.")
        return None
    if q < ZERO:
        r.error(f"{label} cannot be negative.")
        return None
    return q


def _check_file(path: str | Path) -> Path:
    p = Path(path)
    if not p.is_file():
        raise ValidationError(f"File not found: {p}")
    if p.suffix.lower() not in (".xlsx", ".xlsm"):
        raise ValidationError("Choose an Excel workbook (.xlsx). Older .xls files: open them in Excel and "
                              "'Save As' .xlsx first.")
    return p


def _load(path: Path):
    from openpyxl import load_workbook
    try:
        return load_workbook(path, data_only=True, read_only=True)
    except zipfile.BadZipFile:
        raise ValidationError(f"'{path.name}' is not a valid Excel workbook (or it is password protected).") from None
    except PermissionError:
        raise ValidationError(f"'{path.name}' is open in another program. Close it and try again.") from None


def _header_map(values, fields: dict) -> dict[str, int]:
    """Column of each field from a header row: the exact template header first, then other words."""
    norm = [_key(_text(v)).replace(" ", "") for v in values]
    out: dict[str, int] = {}
    used: set[int] = set()
    for exact in (True, False):
        for f, (title, words) in fields.items():
            if f in out:
                continue
            wanted = [_key(title).replace(" ", "")] if exact else [_key(w).replace(" ", "") for w in words]
            for i, h in enumerate(norm):
                if h and i not in used and h in wanted:
                    out[f] = i
                    used.add(i)
                    break
    return out


def _find_header(ws, fields: dict, must: str) -> tuple[int, dict[str, int]] | None:
    for n, values in enumerate(ws.iter_rows(min_row=1, max_row=15, values_only=True), start=1):
        m = _header_map(values, fields)
        if must in m and "project_no" in m:
            return n, m
    return None


def _find_sheet(wb, name: str, fields: dict, must: str, exclude=None):
    names = [ws.title for ws in wb.worksheets]
    ordered = [n for n in names if _key(n) == _key(name)] + [n for n in names if _key(n) != _key(name)
                                                             and _key(n) not in (_key(HELP_SHEET), _key(EXAMPLE_SHEET))]
    for n in ordered:
        ws = wb[n]
        if exclude is not None and ws.title == exclude.title:
            continue
        if _find_header(ws, fields, must) is not None:
            return ws
    return None


def _read(ws, fields: dict, required: tuple[str, ...], problems: list[str]) -> list[Row]:
    header_row, m = _find_header(ws, fields, required[1])
    missing = [fields[f][0] for f in required if f not in m]
    if missing:
        problems.append(f"Sheet '{ws.title}': column(s) {', '.join(missing)} not found on the header row.")
        return []
    rows = []
    for n, values in enumerate(ws.iter_rows(min_row=header_row + 1, values_only=True), start=header_row + 1):
        vals = {f: (_text(values[i]) if i < len(values) else "") for f, i in m.items()}
        if not any(vals.values()):
            continue
        if _key(vals.get("project_no")) in ("total", "разом", "всього"):
            continue
        rows.append(Row(ws.title, n, vals))
    return rows


# ---- the template ---------------------------------------------------------------------------------
def write_template(dest: str | Path) -> Path:
    """The workbook to fill in: Structure + Materials sheets (empty), How to fill in, and an Example."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    dest = Path(dest)
    head = PatternFill("solid", fgColor="1F3A5F")
    must = PatternFill("solid", fgColor="8A1C1C")
    widths = {"project_no": 14, "project_name": 28, "manager": 20, "block": 14, "level": 12, "unit_code": 12,
              "unit_name": 30, "quantity": 11, "notes": 30, "material_code": 20, "qty_per_unit": 14, "vpk_code": 14}

    def sheet(ws, fields: dict, rows=()):
        for i, (f, (title, _w)) in enumerate(fields.items(), start=1):
            c = ws.cell(row=1, column=i, value=title)
            c.font = Font(bold=True, color="FFFFFF")
            c.fill = must if title.endswith("*") else head
            c.alignment = Alignment(vertical="center")
            ws.column_dimensions[get_column_letter(i)].width = widths.get(f, 14)
        for r in rows:
            ws.append(list(r))
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = f"A1:{get_column_letter(len(fields))}{max(1, ws.max_row)}"
        ws.row_dimensions[1].height = 22

    wb = Workbook()
    sheet(wb.active, STRUCTURE_FIELDS)
    wb.active.title = STRUCTURE_SHEET
    sheet(wb.create_sheet(MATERIALS_SHEET), MATERIAL_FIELDS)
    hw = wb.create_sheet(HELP_SHEET)
    hw.append(["Project structure import - how to fill in"])
    hw["A1"].font = Font(bold=True, size=13)
    hw.append([])
    for title, rows in ((f"Sheet '{STRUCTURE_SHEET}' - one row per unit", STRUCTURE_HELP),
                        (f"Sheet '{MATERIALS_SHEET}' (optional) - one row per material of a unit", MATERIAL_HELP)):
        hw.append([title])
        hw.cell(row=hw.max_row, column=1).font = Font(bold=True)
        for col, text in rows:
            hw.append([col, text])
        hw.append([])
    hw.append(["Rules"])
    hw.cell(row=hw.max_row, column=1).font = Font(bold=True)
    for text in RULES:
        hw.append(["•", text])
    hw.append([])
    hw.append(["Columns marked * (red header) are required. See the Example sheet for a filled-in example."])
    hw.column_dimensions["A"].width = 26
    hw.column_dimensions["B"].width = 110
    for row in hw.iter_rows(min_row=2):
        row[1].alignment = Alignment(wrap_text=True, vertical="top")
    ex = wb.create_sheet(EXAMPLE_SHEET)
    ex.append(["Example only - this sheet is never imported."])
    ex["A1"].font = Font(bold=True, italic=True, color="8A1C1C")
    ex.append([STRUCTURE_SHEET])
    ex.cell(row=2, column=1).font = Font(bold=True)
    ex.append([t for t, _w in STRUCTURE_FIELDS.values()])
    for r in EXAMPLE_STRUCTURE:
        ex.append(list(r))
    ex.append([])
    ex.append([MATERIALS_SHEET])
    ex.cell(row=ex.max_row, column=1).font = Font(bold=True)
    ex.append([t for t, _w in MATERIAL_FIELDS.values()])
    for r in EXAMPLE_MATERIALS:
        ex.append(list(r))
    for i in range(1, 10):
        ex.column_dimensions[get_column_letter(i)].width = 18
    for row in (3, 3 + len(EXAMPLE_STRUCTURE) + 3):
        for c in ex[row]:
            c.font = Font(bold=True)
    wb.save(dest)
    return dest


def export_problems(plan: StructurePlan, dest: str | Path) -> Path:
    """The rows with errors or warnings and the reason - to fix in the original workbook."""
    from openpyxl import Workbook
    from openpyxl.styles import Font
    dest = Path(dest)
    wb = Workbook()
    ws = wb.active
    ws.title = "Import problems"
    ws.append([f"Project structure import: {plan.source.name}"])
    ws["A1"].font = Font(bold=True, size=13)
    ws.append([plan.summary()])
    for p in plan.problems:
        ws.append([p])
    ws.append([])
    ws.append(["Sheet", "Excel row", "Status", "Where", "Unit", "Material", "Problem / note"])
    for c in ws[ws.max_row]:
        c.font = Font(bold=True)
    for r in plan.rows:
        if r.errors or r.warnings:
            ws.append([r.sheet, r.row, r.status, r.where, r.get("unit_code") or r.get("unit_name"),
                       r.get("material_code"), r.problems])
    for col, w in zip("ABCDEFG", (12, 10, 10, 26, 24, 18, 90), strict=True):
        ws.column_dimensions[col].width = w
    wb.save(dest)
    return dest


__all__ = ["ERROR", "NEW", "SAME", "UPDATE", "Row", "StructureImportService", "StructurePlan", "StructureResult",
           "export_problems", "write_template"]
