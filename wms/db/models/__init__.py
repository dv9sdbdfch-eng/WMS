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
    StockReturn,
    StockTransfer,
)
from wms.db.models.locations import Bin, Location
from wms.db.models.products import BomItem, FinishedProductInfo, Product
from wms.db.models.reference import Category, Supplier, Unit
from wms.db.models.sites import Site
from wms.db.models.stock import StockLevel, StockMovement
from wms.db.models.system import Attachment, AuditLog, NumberSequence, Setting
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
    "NumberSequence",
    "Product",
    "ProductionLine",
    "ProductionOrder",
    "ReturnLine",
    "Role",
    "RolePermission",
    "Setting",
    "Site",
    "StockAdjustment",
    "StockLevel",
    "StockMovement",
    "StockReturn",
    "StockTransfer",
    "Supplier",
    "Unit",
    "User",
]
