"""Initial data for a brand-new database."""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from wms.core.permissions import DEFAULT_ROLES
from wms.core.security import hash_password
from wms.db.models import Bin, Category, Location, NumberSequence, Role, RolePermission, Unit, User

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
]


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
