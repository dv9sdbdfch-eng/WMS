"""Shared fixtures: a fresh database per test, logged in as the seeded admin."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from wms.config import AppConfig
from wms.db.engine import Database
from wms.db.seed import seed_if_empty
from wms.services import Services
from wms.services.base import AppContext


@pytest.fixture(autouse=True)
def _quiet_ui(tmp_path):
    """Screens: run background work inline (deterministic tests) and keep table settings
    (column choices) in the test folder, not in the real user profile."""
    try:
        from PySide6.QtCore import QSettings

        from wms.ui.widgets import tasks
    except ImportError:  # pragma: no cover - service tests without Qt
        yield
        return
    tasks.SYNC = True
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "qsettings"))
    yield


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("WMS_DATA_DIR", str(tmp_path / "data"))
    c = AppConfig.load()
    c.ensure_dirs()
    return c


@pytest.fixture
def svc(cfg) -> Services:
    db = Database(cfg.resolved_database_url)
    db.migrate()
    with db.session() as s:
        seed_if_empty(s)
    ctx = AppContext(db=db, config=cfg)
    services = Services(ctx)
    services.auth.login("admin", "admin")
    yield services
    db.dispose()


class Sample:
    """A small warehouse: two bins, a supplier, a site, three products."""

    def __init__(self, svc: Services):
        self.svc = svc
        units = {u.code: u for u in svc.reference.units()}
        self.main = next(l for l in svc.locations.tree() if l.code == "MAIN")
        self.zone = svc.locations.save_location(None, {"kind": "ZONE", "code": "A", "name": "Zone A",
                                                       "parent_id": self.main.id})
        self.rack = svc.locations.save_location(None, {"kind": "RACK", "code": "R03", "parent_id": self.zone.id})
        self.shelf = svc.locations.save_location(None, {"kind": "SHELF", "code": "S02", "parent_id": self.rack.id})
        self.bin1 = svc.locations.save_bin(None, {"location_id": self.shelf.id, "code": "B04"})
        self.bin2 = svc.locations.save_bin(None, {"location_id": self.shelf.id, "code": "B05"})
        self.quarantine = next(b for b in svc.locations.bins() if b.code == "QUARANTINE")
        self.supplier = svc.reference.save_supplier(None, {"code": "EGG", "name": "Egger UK Ltd"})
        self.site = svc.sites.save(None, {"project_number": "P-1001", "name": "Riverside Apartments",
                                          "customer": "Acme Homes", "status": "ACTIVE"})
        self.mdf = svc.products.save(None, {"item_type": "RAW_MATERIAL", "internal_code": "mdf-18",
                                            "name": "MDF 18mm 2440x1220", "unit_id": units["SHEET"].id,
                                            "supplier_id": self.supplier.id, "supplier_code": "EG-MDF18",
                                            "default_bin_id": self.bin1.id, "min_stock": "10", "unit_cost": "24.50"})
        self.edge = svc.products.save(None, {"item_type": "RAW_MATERIAL", "internal_code": "EDGE-W22",
                                             "name": "ABS edging white 22mm", "unit_id": units["M"].id,
                                             "default_bin_id": self.bin2.id, "unit_cost": "0.18"})
        self.cabinet = svc.products.save(None, {"item_type": "FINISHED", "internal_code": "CAB-600",
                                                "name": "Base cabinet 600", "unit_id": units["EA"].id,
                                                "default_bin_id": self.bin1.id, "status": "READY"})

    def receive(self, product, qty, bin_=None, number="DN-1", cost=None):
        from wms.services.goods_in import ReceiptLine
        return self.svc.goods_in.post(
            {"dn_number": number, "supplier_id": self.supplier.id, "delivery_date": date.today()},
            [ReceiptLine(product.id, Decimal(str(qty)), (bin_ or product.default_bin).id if (bin_ or product.default_bin) else None, cost)],
        )


@pytest.fixture
def sample(svc) -> Sample:
    return Sample(svc)


def pytest_sessionfinish(session, exitstatus):
    """Destroy every window while Qt is still running.

    Screens left open by the UI tests were otherwise destroyed during Python's own shutdown, after
    the QApplication, which now and then crashed the process (exit code 139 / 0xC0000005) after
    all tests had passed - and that fails the CI step.
    """
    import gc
    import sys
    if "PySide6.QtWidgets" not in sys.modules:
        return
    from PySide6.QtCore import QCoreApplication, QEvent
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance()
    if app is None:
        return
    for w in app.topLevelWidgets():
        w.close()
        w.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()
    gc.collect()
