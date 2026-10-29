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
    STOCKTAKE_ADJUSTMENT = "STOCKTAKE_ADJUSTMENT"
    # Never stored: a reversal keeps the type of the movement it reverses (so every "Goods Out"
    # total nets off automatically) and is marked by reversal_of_id + a movement_reversals row.
    # Used as a filter / display value only. See docs/STOCK_LEDGER.md.
    REVERSAL = "REVERSAL"


# Names used in the Phase 1 specification for the stored movement types above. Stored values
# never change once data exists, so the specification names are aliases.
MOVEMENT_TYPE_ALIASES = {
    "BIN_TRANSFER": MovementType.TRANSFER,
    "SITE_RETURN": MovementType.RETURN_FROM_SITE,
    "SUPPLIER_RETURN": MovementType.RETURN_TO_SUPPLIER,
    "PRODUCTION_CONSUMPTION": MovementType.PRODUCTION_CONSUME,
}


class DocumentStatus(LabelledEnum):
    POSTED = "POSTED"
    REVERSED = "REVERSED"


class CountStatus(LabelledEnum):
    OPEN = "OPEN"
    SUBMITTED = "SUBMITTED"
    APPROVED = "APPROVED"
    CANCELLED = "CANCELLED"


class StockStatus(LabelledEnum):
    OK = "OK"
    LOW = "LOW"
    OUT = "OUT"
    NO_MINIMUM = "NO_MINIMUM"


class SiteStatus(LabelledEnum):
    PLANNED = "PLANNED"
    ACTIVE = "ACTIVE"
    ON_HOLD = "ON_HOLD"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    ARCHIVED = "ARCHIVED"


# Projects that can receive new Goods Out / production. Completed, cancelled and archived projects
# keep their full history (and stay searchable and reportable) but are hidden from normal selection.
OPEN_SITE_STATUSES = (SiteStatus.PLANNED.value, SiteStatus.ACTIVE.value, SiteStatus.ON_HOLD.value)
CLOSED_SITE_STATUSES = (SiteStatus.COMPLETED.value, SiteStatus.CANCELLED.value, SiteStatus.ARCHIVED.value)


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


class ReasonUse(LabelledEnum):
    """Where a reason code may be chosen."""
    ADJUSTMENT = "ADJUSTMENT"
    REVERSAL = "REVERSAL"
    RETURN = "RETURN"
    DISCREPANCY = "DISCREPANCY"


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
    STOCK_COUNT = "STOCK_COUNT"
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
    MovementType.STOCKTAKE_ADJUSTMENT: "Stocktake Adjustment",
    MovementType.REVERSAL: "Reversal",
    SiteStatus.ON_HOLD: "On Hold",
    FinishedStatus.IN_PRODUCTION: "In Production",
    FinishedStatus.ON_HOLD: "On Hold",
    ReturnType.FROM_SITE: "Return from Site",
    ReturnType.TO_SUPPLIER: "Return to Supplier",
    AdjustmentReason.STOCK_COUNT: "Stock Count Difference",
    AdjustmentReason.DATA_ENTRY_ERROR: "Data Entry Error",
    AdjustmentReason.OPENING_BALANCE: "Opening Balance",
    EntityType.GOODS_ISSUE: "Goods Out",
    EntityType.STOCK_COUNT: "Stocktake",
    CountStatus.SUBMITTED: "Awaiting Approval",
    StockStatus.OUT: "Out of Stock",
    StockStatus.NO_MINIMUM: "No Minimum Set",
    EntityType.DELIVERY_NOTE: "Delivery Note",
}
