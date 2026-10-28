"""Suppliers, categories and units of measure."""

from __future__ import annotations

from sqlalchemy import func, or_, select

from wms.core.exceptions import DuplicateError, NotFoundError, ValidationError
from wms.core.permissions import P
from wms.db.models import Category, Product, Supplier, Unit
from wms.services import audit
from wms.services.base import BaseService, clean_str, normalise_code

SUPPLIER_FIELDS = ("code", "name", "contact_name", "phone", "email", "address", "account_number", "notes", "is_active")


class ReferenceService(BaseService):
    # -- suppliers ----------------------------------------------------------
    def suppliers(self, text: str | None = None, include_inactive: bool = False) -> list[Supplier]:
        with self.db.read_session() as s:
            q = select(Supplier).order_by(Supplier.name)
            if not include_inactive:
                q = q.where(Supplier.is_active.is_(True))
            if text:
                like = f"%{text.strip()}%"
                q = q.where(or_(Supplier.name.ilike(like), Supplier.code.ilike(like), Supplier.contact_name.ilike(like)))
            return list(s.scalars(q))

    def save_supplier(self, supplier_id: int | None, data: dict) -> Supplier:
        self.require(P.SUPPLIERS_EDIT)
        code = normalise_code(data.get("code"))
        if not code:
            raise ValidationError("Supplier code is required.", "code")
        name = clean_str(data.get("name"), "name", required=True, max_len=200)
        with self.db.session() as s:
            dup = s.scalar(select(Supplier.id).where(func.upper(Supplier.code) == code))
            if dup and dup != supplier_id:
                raise DuplicateError(f"Supplier code '{code}' already exists.")
            if supplier_id:
                sup = s.get(Supplier, supplier_id)
                if sup is None:
                    raise NotFoundError("Supplier not found.")
                old = audit.snapshot(sup, SUPPLIER_FIELDS)
            else:
                sup = Supplier()
                s.add(sup)
                old = {}
            sup.code = code
            sup.name = name
            for f in ("contact_name", "phone", "email", "address", "account_number", "notes"):
                setattr(sup, f, clean_str(data.get(f)))
            sup.is_active = bool(data.get("is_active", True))
            s.flush()
            o, n = audit.diff(old, audit.snapshot(sup, SUPPLIER_FIELDS))
            if n:
                audit.record(s, self.ctx, "SUPPLIER_UPDATED" if supplier_id else "SUPPLIER_CREATED",
                             "SUPPLIER", sup.id, f"{sup.code} {sup.name}", old=o, new=n)
            return sup

    # -- categories ---------------------------------------------------------
    def categories(self, include_inactive: bool = False) -> list[Category]:
        with self.db.read_session() as s:
            q = select(Category)
            if not include_inactive:
                q = q.where(Category.is_active.is_(True))
            return sorted(s.scalars(q).unique(), key=lambda c: c.full_name.lower())

    def save_category(self, category_id: int | None, name: str, parent_id: int | None = None,
                      is_active: bool = True) -> Category:
        self.require(P.SUPPLIERS_EDIT)
        name = clean_str(name, "name", required=True, max_len=100)
        with self.db.session() as s:
            if parent_id and category_id and parent_id == category_id:
                raise ValidationError("A category cannot be its own parent.")
            dup = s.scalar(select(Category.id).where(func.lower(Category.name) == name.lower(),
                                                      Category.parent_id.is_(parent_id) if parent_id is None
                                                      else Category.parent_id == parent_id))
            if dup and dup != category_id:
                raise DuplicateError(f"Category '{name}' already exists here.")
            cat = s.get(Category, category_id) if category_id else Category()
            if cat is None:
                raise NotFoundError("Category not found.")
            if not category_id:
                s.add(cat)
            old = {"name": cat.name, "parent_id": cat.parent_id, "is_active": cat.is_active}
            cat.name, cat.parent_id, cat.is_active = name, parent_id, is_active
            s.flush()
            o, n = audit.diff(old, {"name": cat.name, "parent_id": cat.parent_id, "is_active": cat.is_active})
            if n:
                audit.record(s, self.ctx, "CATEGORY_SAVED", "CATEGORY", cat.id, name, old=o, new=n)
            saved_id = cat.id
        return self.reload(Category, saved_id)

    # -- units --------------------------------------------------------------
    def units(self, include_inactive: bool = False) -> list[Unit]:
        with self.db.read_session() as s:
            q = select(Unit).order_by(Unit.code)
            if not include_inactive:
                q = q.where(Unit.is_active.is_(True))
            return list(s.scalars(q))

    def save_unit(self, unit_id: int | None, code: str, name: str, allow_decimal: bool, is_active: bool = True) -> Unit:
        self.require(P.SUPPLIERS_EDIT)
        code = normalise_code(code)
        if not code:
            raise ValidationError("Unit code is required.", "code")
        name = clean_str(name, "name", required=True, max_len=50)
        with self.db.session() as s:
            dup = s.scalar(select(Unit.id).where(Unit.code == code))
            if dup and dup != unit_id:
                raise DuplicateError(f"Unit '{code}' already exists.")
            unit = s.get(Unit, unit_id) if unit_id else Unit()
            if unit is None:
                raise NotFoundError("Unit not found.")
            if unit_id and unit.allow_decimal and not allow_decimal:
                in_use = s.scalar(select(func.count(Product.id)).where(Product.unit_id == unit_id))
                if in_use:
                    raise ValidationError("Cannot switch a unit in use to whole numbers only.")
            if not unit_id:
                s.add(unit)
            unit.code, unit.name, unit.allow_decimal, unit.is_active = code, name, allow_decimal, is_active
            s.flush()
            audit.record(s, self.ctx, "UNIT_SAVED", "UNIT", unit.id, code)
            return unit
