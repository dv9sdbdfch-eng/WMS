"""Enumerations shared across layers.

Values are stored in the database as their string value, so they must never be
renamed once data exists. Labels are what the UI shows.
"""

from __future__ import annotations

from enum import Enum


class LabelledEnum(str, Enum):
    @property
    def label(self) -> str:
        return _LABELS.get(self, self.value.replace("_", " ").title())

    @classmethod
    def choices(cls) -> list[tuple[str, str]]:
        return [(m.value, m.label) for m in cls]


class ItemType(LabelledEnum):
    RAW_MATERIAL = "RAW_MATERIAL"
    COMPONENT = "COMPONENT"
    CONSUMABLE = "CONSUMABLE"
    FINISHED = "FINISHED"


class LocationKind(LabelledEnum):
    WAREHOUSE = "WAREHOUSE"
    ZONE = "ZONE"
    RACK = "RACK"
    SHELF = "SHELF"


class MovementType(LabelledEnum):
    GOODS_IN = "GOODS_IN"
    GOODS_OUT = "GOODS_OUT"
    TRANSFER = "TRANSFER"
    ADJUSTMENT_IN = "ADJUSTMENT_IN"
    ADJUSTMENT_OUT = "ADJUSTMENT_OUT"
    RETURN_FROM_SITE = "RETURN_FROM_SITE"
    RETURN_TO_SUPPLIER = "RETURN_TO_SUPPLIER"
    PRODUCTION_OUTPUT = "PRODUCTION_OUTPUT"
    PRODUCTION_CONSUME = "PRODUCTION_CONSUME"
    OPENING_BALANCE = "OPENING_BALANCE"


class DocumentStatus(LabelledEnum):
    POSTED = "POSTED"
    REVERSED = "REVERSED"


class SiteStatus(LabelledEnum):
    PLANNED = "PLANNED"
    ACTIVE = "ACTIVE"
    ON_HOLD = "ON_HOLD"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class FinishedStatus(LabelledEnum):
    IN_PRODUCTION = "IN_PRODUCTION"
    READY = "READY"
    RESERVED = "RESERVED"
    DISPATCHED = "DISPATCHED"
    ON_HOLD = "ON_HOLD"


class ReturnType(LabelledEnum):
    FROM_SITE = "FROM_SITE"
    TO_SUPPLIER = "TO_SUPPLIER"


class ReturnCondition(LabelledEnum):
    USABLE = "USABLE"
    DAMAGED = "DAMAGED"


class AdjustmentReason(LabelledEnum):
    STOCK_COUNT = "STOCK_COUNT"
    DAMAGED = "DAMAGED"
    LOST = "LOST"
    FOUND = "FOUND"
    DATA_ENTRY_ERROR = "DATA_ENTRY_ERROR"
    OPENING_BALANCE = "OPENING_BALANCE"
    OTHER = "OTHER"


class AttachmentKind(LabelledEnum):
    PHOTO = "PHOTO"
    DOCUMENT = "DOCUMENT"


class EntityType(LabelledEnum):
    PRODUCT = "PRODUCT"
    DELIVERY_NOTE = "DELIVERY_NOTE"
    GOODS_ISSUE = "GOODS_ISSUE"
    SITE = "SITE"
    RETURN = "RETURN"
    ADJUSTMENT = "ADJUSTMENT"
    TRANSFER = "TRANSFER"
    PRODUCTION = "PRODUCTION"
    SUPPLIER = "SUPPLIER"
    CATEGORY = "CATEGORY"
    UNIT = "UNIT"
    LOCATION = "LOCATION"
    BIN = "BIN"
    USER = "USER"
    SETTING = "SETTING"
    SYSTEM = "SYSTEM"


# Movement types that add stock to the "to" bin / remove from the "from" bin.
INBOUND_TYPES = {
    MovementType.GOODS_IN,
    MovementType.ADJUSTMENT_IN,
    MovementType.RETURN_FROM_SITE,
    MovementType.PRODUCTION_OUTPUT,
    MovementType.OPENING_BALANCE,
}
OUTBOUND_TYPES = {
    MovementType.GOODS_OUT,
    MovementType.ADJUSTMENT_OUT,
    MovementType.RETURN_TO_SUPPLIER,
    MovementType.PRODUCTION_CONSUME,
}

_LABELS = {
    ItemType.RAW_MATERIAL: "Raw Material",
    ItemType.COMPONENT: "Component",
    ItemType.CONSUMABLE: "Consumable",
    ItemType.FINISHED: "Finished Product",
    MovementType.GOODS_IN: "Goods In",
    MovementType.GOODS_OUT: "Goods Out",
    MovementType.TRANSFER: "Bin Transfer",
    MovementType.ADJUSTMENT_IN: "Adjustment (+)",
    MovementType.ADJUSTMENT_OUT: "Adjustment (-)",
    MovementType.RETURN_FROM_SITE: "Return from Site",
    MovementType.RETURN_TO_SUPPLIER: "Return to Supplier",
    MovementType.PRODUCTION_OUTPUT: "Finished Product Added",
    MovementType.PRODUCTION_CONSUME: "Consumed in Production",
    MovementType.OPENING_BALANCE: "Opening Balance",
    SiteStatus.ON_HOLD: "On Hold",
    FinishedStatus.IN_PRODUCTION: "In Production",
    FinishedStatus.ON_HOLD: "On Hold",
    ReturnType.FROM_SITE: "Return from Site",
    ReturnType.TO_SUPPLIER: "Return to Supplier",
    AdjustmentReason.STOCK_COUNT: "Stock Count Difference",
    AdjustmentReason.DATA_ENTRY_ERROR: "Data Entry Error",
    AdjustmentReason.OPENING_BALANCE: "Opening Balance",
    EntityType.GOODS_ISSUE: "Goods Out",
    EntityType.DELIVERY_NOTE: "Delivery Note",
}
