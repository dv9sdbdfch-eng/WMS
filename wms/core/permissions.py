"""Permission catalogue and default role assignments.

Permissions are plain strings stored in ``role_permissions``. The defaults
below are only used to seed a new database; after that an administrator owns
them, so adding a new role later needs no code change.
"""

from __future__ import annotations


class P:
    PRODUCTS_VIEW = "products.view"
    PRODUCTS_EDIT = "products.edit"
    SUPPLIERS_EDIT = "suppliers.edit"
    LOCATIONS_VIEW = "locations.view"
    LOCATIONS_EDIT = "locations.edit"
    GOODS_IN_VIEW = "goods_in.view"
    GOODS_IN_POST = "goods_in.post"
    GOODS_IN_REVERSE = "goods_in.reverse"
    PURCHASING_VIEW = "purchasing.view"
    PURCHASING_EDIT = "purchasing.edit"
    GOODS_OUT_VIEW = "goods_out.view"
    GOODS_OUT_POST = "goods_out.post"
    GOODS_OUT_REVERSE = "goods_out.reverse"
    SITES_VIEW = "sites.view"
    SITES_EDIT = "sites.edit"
    STOCK_VIEW = "stock.view"
    STOCK_TRANSFER = "stock.transfer"
    STOCK_ADJUST = "stock.adjust"
    STOCK_OVERRIDE_NEGATIVE = "stock.override_negative"
    STOCKTAKE_COUNT = "stocktake.count"
    RETURNS_POST = "returns.post"
    PRODUCTION_POST = "production.post"
    ATTACHMENTS_ADD = "attachments.add"
    ATTACHMENTS_DELETE = "attachments.delete"
    REPORTS_VIEW = "reports.view"
    REPORTS_EXPORT = "reports.export"
    AUDIT_VIEW = "audit.view"
    USERS_MANAGE = "users.manage"
    SETTINGS_MANAGE = "settings.manage"
    BACKUP_RUN = "backup.run"
    BACKUP_RESTORE = "backup.restore"


ALL_PERMISSIONS: list[str] = sorted(v for k, v in vars(P).items() if k.isupper())

PERMISSION_LABELS = {
    P.PRODUCTS_VIEW: "View products",
    P.PRODUCTS_EDIT: "Create and edit products",
    P.SUPPLIERS_EDIT: "Manage suppliers, categories and units",
    P.LOCATIONS_VIEW: "View locations and bins",
    P.LOCATIONS_EDIT: "Manage locations and bins",
    P.GOODS_IN_VIEW: "View delivery notes",
    P.GOODS_IN_POST: "Post Goods In",
    P.GOODS_IN_REVERSE: "Reverse delivery notes",
    P.PURCHASING_VIEW: "View purchase orders and delivery problems",
    P.PURCHASING_EDIT: "Create purchase orders, resolve delivery problems",
    P.GOODS_OUT_VIEW: "View Goods Out",
    P.GOODS_OUT_POST: "Post Goods Out",
    P.GOODS_OUT_REVERSE: "Reverse Goods Out",
    P.SITES_VIEW: "View sites / projects",
    P.SITES_EDIT: "Manage sites / projects",
    P.STOCK_VIEW: "View stock and movements",
    P.STOCK_TRANSFER: "Transfer stock between bins",
    P.STOCK_ADJUST: "Stock adjustments",
    P.STOCK_OVERRIDE_NEGATIVE: "Override negative stock",
    P.STOCKTAKE_COUNT: "Stocktake: create counts and enter quantities (approval needs Stock adjustments)",
    P.RETURNS_POST: "Post returns",
    P.PRODUCTION_POST: "Record production",
    P.ATTACHMENTS_ADD: "Add photos and documents",
    P.ATTACHMENTS_DELETE: "Remove photos and documents",
    P.REPORTS_VIEW: "View reports",
    P.REPORTS_EXPORT: "Export reports",
    P.AUDIT_VIEW: "View audit trail",
    P.USERS_MANAGE: "Manage users and roles",
    P.SETTINGS_MANAGE: "Change settings",
    P.BACKUP_RUN: "Create backups",
    P.BACKUP_RESTORE: "Restore backups",
}

_VIEW = [p for p in ALL_PERMISSIONS if p.endswith(".view") and p != P.AUDIT_VIEW]

DEFAULT_ROLES: dict[str, tuple[str, str, list[str]]] = {
    "ADMIN": ("Administrator", "Full access.", ALL_PERMISSIONS),
    "MANAGER": (
        "Store Manager",
        "Goods In, Goods Out, stock movements, products, locations and reports.",
        [p for p in ALL_PERMISSIONS if p not in {P.USERS_MANAGE, P.BACKUP_RESTORE, P.SETTINGS_MANAGE}],
    ),
    "OPERATIVE": (
        "Warehouse Operative",
        "Day-to-day receiving, issuing and bin transfers.",
        _VIEW
        + [
            P.GOODS_IN_POST,
            P.GOODS_OUT_POST,
            P.STOCK_TRANSFER,
            P.STOCKTAKE_COUNT,
            P.RETURNS_POST,
            P.ATTACHMENTS_ADD,
        ],
    ),
    "VIEWER": ("Viewer", "Read-only access.", list(_VIEW)),
}
