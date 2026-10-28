# PyInstaller build definition. Run from the project folder:
#     pyinstaller packaging\StoreWMS.spec --noconfirm
# Produces dist\StoreWMS\StoreWMS.exe (a folder build: starts fast, no unpacking).

from pathlib import Path

ROOT = Path(SPECPATH).parent

# Qt modules the program never uses. Leaving them out makes the installer far smaller.
EXCLUDES = [
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick", "PySide6.QtWebChannel",
    "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.Qt3DExtras", "PySide6.QtQuick", "PySide6.QtQuick3D",
    "PySide6.QtQml", "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets", "PySide6.QtCharts",
    "PySide6.QtDataVisualization", "PySide6.QtBluetooth", "PySide6.QtNfc", "PySide6.QtPositioning",
    "PySide6.QtLocation", "PySide6.QtSensors", "PySide6.QtSerialPort", "PySide6.QtSql", "PySide6.QtTest",
    "PySide6.QtDesigner", "PySide6.QtHelp", "PySide6.QtRemoteObjects", "PySide6.QtScxml", "PySide6.QtSpatialAudio",
    "PySide6.QtTextToSpeech", "PySide6.QtWebSockets", "PySide6.QtHttpServer", "PySide6.QtGraphs",
    "tkinter", "pytest", "pytestqt",
]

a = Analysis(
    [str(ROOT / "run.py")],
    pathex=[str(ROOT)],
    datas=[
        (str(ROOT / "wms" / "db" / "migrations"), "wms/db/migrations"),
        (str(ROOT / "wms" / "resources" / "icon.ico"), "wms/resources"),
        (str(ROOT / "wms" / "resources" / "icon.png"), "wms/resources"),
    ],
    hiddenimports=[
        "sqlalchemy.dialects.sqlite",
        "logging.config",
        "PySide6.QtPdf",
        "PySide6.QtPdfWidgets",
        "reportlab.graphics.barcode.common",
        "reportlab.graphics.barcode.code128",
        "reportlab.graphics.barcode.code39",
        "reportlab.graphics.barcode.code93",
        "reportlab.graphics.barcode.usps",
        "reportlab.graphics.barcode.usps4s",
        "reportlab.graphics.barcode.ecc200datamatrix",
    ],
    excludes=EXCLUDES,
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="StoreWMS",
    icon=str(ROOT / "wms" / "resources" / "icon.ico"),
    console=False,
    version=str(ROOT / "packaging" / "version_info.txt"),
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="StoreWMS")
