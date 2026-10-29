"""All ORM models. Importing this package registers every table on Base.metadata."""

from wms.db.models.documents import (
    DeliveryNote,
    DeliveryNoteLine,
    GoodsIssue,
    GoodsIssueLine,
    ProductionLine,
    ProductionOrder,
    ReturnLine,
    StockAdjustment,
    StockCount,
    StockCountLine,
    StockReturn,
    StockTransfer,
)
from wms.db.models.locations import Bin, Location
from wms.db.models.products import BomItem, FinishedProductInfo, Product
from wms.db.models.reference import Category, ReasonCode, Supplier, Unit
from wms.db.models.sites import Site
from wms.db.models.stock import MovementReversal, StockLevel, StockMovement
from wms.db.models.system import Attachment, AuditLog, NumberSequence, RecentSearch, SavedSearch, Setting
from wms.db.models.users import Role, RolePermission, User

__all__ = [
    "Attachment",
    "AuditLog",
    "Bin",
    "BomItem",
    "Category",
    "DeliveryNote",
    "DeliveryNoteLine",
    "FinishedProductInfo",
    "GoodsIssue",
    "GoodsIssueLine",
    "Location",
    "MovementReversal",
    "NumberSequence",
    "Product",
    "ProductionLine",
    "ProductionOrder",
    "ReasonCode",
    "RecentSearch",
    "ReturnLine",
    "Role",
    "RolePermission",
    "SavedSearch",
    "Setting",
    "Site",
    "StockAdjustment",
    "StockCount",
    "StockCountLine",
    "StockLevel",
    "StockMovement",
    "StockReturn",
    "StockTransfer",
    "Supplier",
    "Unit",
    "User",
]
