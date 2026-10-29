"""Initial data for a brand-new database."""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from wms.core.permissions import DEFAULT_ROLES
from wms.core.security import hash_password
from wms.db.models import Bin, Category, Location, NumberSequence, ReasonCode, Role, RolePermission, Unit, User

log = logging.getLogger(__name__)

DEFAULT_ADMIN_USERNAME = "admin"
DEFAULT_ADMIN_PASSWORD = "admin"

UNITS = [
    ("EA", "Each", False),
    ("PAIR", "Pair", False),
    ("BOX", "Box", False),
    ("PACK", "Pack", False),
    ("SHEET", "Sheet", False),
    ("ROLL", "Roll", False),
    ("M", "Metre", True),
    ("LM", "Linear Metre", True),
    ("M2", "Square Metre", True),
    ("M3", "Cubic Metre", True),
    ("KG", "Kilogram", True),
    ("L", "Litre", True),
]

CATEGORIES = [
    "Sheet Materials",
    "Timber",
    "Edging",
    "Hardware & Fittings",
    "Fixings",
    "Adhesives & Finishes",
    "Worktops",
    "Packaging",
    "Components",
    "Finished Furniture",
]

SEQUENCES = [
    ("GOODS_ISSUE", "GO-"),
    ("RETURN_FROM_SITE", "RS-"),
    ("RETURN_TO_SUPPLIER", "RT-"),
    ("ADJUSTMENT", "ADJ-"),
    ("TRANSFER", "TRF-"),
    ("PRODUCTION", "PRD-"),
    ("STOCK_COUNT", "SC-"),
    ("PURCHASE_ORDER", "PO-"),
    ("DISCREPANCY", "DSC-"),
    ("RESERVATION", "RES-"),
]


# code, label, adjustment, reversal, return, discrepancy, system, sort - same list as migration 0005
REASON_CODES = [
    ("DAMAGED", "Damaged", 1, 1, 1, 1, 0, 10),
    ("MISSING", "Missing", 1, 0, 0, 1, 0, 20),
    ("STOCK_COUNT", "Stocktake Correction", 1, 0, 0, 0, 1, 30),
    ("INCORRECT_DELIVERY", "Incorrect Delivery", 0, 1, 1, 1, 0, 40),
    ("OVER_DELIVERED", "Over-delivered", 0, 0, 0, 1, 0, 45),
    ("REJECTED", "Rejected", 0, 0, 1, 1, 0, 46),
    ("RETURNED_FROM_PROJECT", "Returned from Project", 0, 0, 1, 0, 0, 50),
    ("SUPPLIER_RETURN", "Supplier Return", 0, 0, 1, 0, 0, 60),
    ("SCRAP", "Scrap", 1, 0, 0, 0, 0, 70),
    ("LOST", "Lost", 1, 0, 0, 0, 0, 75),
    ("FOUND", "Found", 1, 0, 0, 0, 0, 76),
    ("DATA_ENTRY_ERROR", "Data Entry Correction", 1, 1, 0, 0, 1, 80),
    ("POSTED_TWICE", "Posted Twice", 0, 1, 0, 0, 0, 85),
    ("OPENING_BALANCE", "Opening Balance", 1, 0, 0, 0, 1, 90),
    ("OTHER", "Other", 1, 1, 1, 1, 1, 999),
]


def seed_reason_codes(session: Session) -> None:
    """Default reason codes for a new database; system codes are put back if ever missing."""
    have = set(session.scalars(select(ReasonCode.code)))
    fresh = not have
    for code, label, adj, rev, ret, disc, system, sort in REASON_CODES:
        if code not in have and (fresh or system):
            session.add(ReasonCode(code=code, label=label, for_adjustment=bool(adj), for_reversal=bool(rev),
                                   for_return=bool(ret), for_discrepancy=bool(disc), is_system=bool(system),
                                   sort_order=sort))
    session.flush()


def seed_roles(session: Session) -> None:
    for code, (name, description, perms) in DEFAULT_ROLES.items():
        role = session.scalar(select(Role).where(Role.code == code))
        if role is None:
            role = Role(code=code, name=name, description=description, is_system=True)
            role.permissions = [RolePermission(permission=p) for p in perms]
            session.add(role)
        elif code == "ADMIN":
            # Administrators always get every permission, including ones added in new versions.
            have = role.permission_set
            for p in perms:
                if p not in have:
                    role.permissions.append(RolePermission(permission=p))
    session.flush()


def seed_if_empty(session: Session) -> bool:
    """Seed reference data. Returns True if this was a fresh database."""
    seed_roles(session)
    seed_reason_codes(session)
    for name, prefix in SEQUENCES:
        if session.get(NumberSequence, name) is None:
            session.add(NumberSequence(name=name, prefix=prefix, next_value=1, padding=6))

    if session.scalar(select(User.id).limit(1)) is not None:
        return False

    log.info("Seeding new database")
    admin_role = session.scalar(select(Role).where(Role.code == "ADMIN"))
    session.add(
        User(
            username=DEFAULT_ADMIN_USERNAME,
            full_name="Administrator",
            password_hash=hash_password(DEFAULT_ADMIN_PASSWORD),
            role=admin_role,
            must_change_password=True,
        )
    )
    for code, name, dec in UNITS:
        session.add(Unit(code=code, name=name, allow_decimal=dec))
    for name in CATEGORIES:
        session.add(Category(name=name))

    main = Location(kind="WAREHOUSE", code="MAIN", name="Main Warehouse", path="MAIN")
    session.add(main)
    session.flush()
    session.add(Bin(location=main, code="GENERAL", description="General storage", full_code="MAIN / GENERAL"))
    session.add(Bin(location=main, code="GOODS-IN", description="Goods In bay", full_code="MAIN / GOODS-IN"))
    session.add(
        Bin(
            location=main,
            code="QUARANTINE",
            description="Damaged / returned goods awaiting inspection",
            full_code="MAIN / QUARANTINE",
            is_available=False,
        )
    )
    session.flush()
    return True
