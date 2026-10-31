"""Visual style: a calm, high-contrast business look. No animations."""

from __future__ import annotations

PRIMARY = "#1F3A5F"
ACCENT = "#2F6FB2"
DANGER = "#B42318"
WARNING = "#B54708"
SUCCESS = "#067647"
LOW_STOCK_BG = "#FDECEA"
NEGATIVE_FG = "#B42318"
MUTED = "#667085"

TEXT = "#101828"
WINDOW_BG = "#F5F7FA"

QSS = f"""
* {{ font-family: "Segoe UI", "Noto Sans", "DejaVu Sans", sans-serif; font-size: 10pt; }}
QWidget {{ color: {TEXT}; }}
QMainWindow, QDialog, QMessageBox, QInputDialog {{ background: {WINDOW_BG}; }}
QMenu {{ background: white; color: {TEXT}; border: 1px solid #D0D5DD; }}
QMenu::item:selected {{ background: #D6E6F7; }}
QToolTip {{ background: white; color: {TEXT}; border: 1px solid #D0D5DD; }}
QComboBox QAbstractItemView {{ background: white; color: {TEXT}; selection-background-color: #D6E6F7;
    selection-color: {TEXT}; }}
QCheckBox, QRadioButton, QLabel {{ background: transparent; }}
QCheckBox::indicator {{ width: 16px; height: 16px; border: 1px solid #98A2B3; border-radius: 3px; background: white; }}
QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; }}
QCheckBox::indicator:disabled {{ background: #F2F4F7; border-color: #D0D5DD; }}
QCalendarWidget QWidget {{ background: white; color: {TEXT}; }}
QWidget#Sidebar {{ background: {PRIMARY}; }}
QListWidget#Nav {{ background: transparent; border: none; color: #DCE3EC; outline: 0; }}
QListWidget#Nav::item {{ padding: 5px 14px; border-radius: 6px; margin: 0px 8px; }}
QListWidget#Nav::item:selected {{ background: #2E5583; color: white; }}
QListWidget#Nav::item:hover:!selected {{ background: #284A73; }}
QLabel#AppTitle {{ color: white; font-size: 13pt; font-weight: 600; padding: 14px 16px 4px 16px; }}
QLabel#AppSubtitle {{ color: #AFC0D3; padding: 0 16px 10px 16px; font-size: 9pt; }}
QLabel#NavSection {{ color: #8FA6BF; font-size: 8pt; font-weight: 600; padding: 10px 22px 2px 22px; }}
QWidget#TopBar {{ background: white; border-bottom: 1px solid #E4E7EC; }}
QLabel#PageTitle {{ font-size: 15pt; font-weight: 600; color: #101828; }}
QLabel#PageSubtitle {{ color: {MUTED}; }}
QLabel#Muted {{ color: {MUTED}; }}
QLabel#SectionTitle {{ font-size: 11pt; font-weight: 600; color: #101828; padding-top: 6px; }}
QFrame#Card {{ background: white; border: 1px solid #E4E7EC; border-radius: 8px; }}
QLabel#CardValue {{ font-size: 18pt; font-weight: 600; color: #101828; }}
QLabel#CardLabel {{ color: {MUTED}; font-size: 9pt; }}
QLabel#CardValueWarn {{ font-size: 18pt; font-weight: 600; color: {DANGER}; }}
QLabel#Banner {{ background: #FEF3F2; color: {DANGER}; border: 1px solid #FECDCA; border-radius: 6px; padding: 8px; }}
QLabel#InfoBanner {{ background: #EFF8FF; color: #175CD3; border: 1px solid #B2DDFF; border-radius: 6px; padding: 8px; }}
QLabel#OkBanner {{ background: #ECFDF3; color: {SUCCESS}; border: 1px solid #ABEFC6; border-radius: 6px; padding: 8px; }}
QPushButton {{ background: white; border: 1px solid #D0D5DD; border-radius: 6px; padding: 6px 14px; color: #344054; }}
QPushButton:hover {{ background: #F9FAFB; }}
QPushButton:disabled {{ color: #98A2B3; background: #F2F4F7; }}
QPushButton#Primary {{ background: {ACCENT}; border-color: {ACCENT}; color: white; font-weight: 600; }}
QPushButton#Primary:hover {{ background: #285F99; }}
QPushButton#Primary:disabled {{ background: #9DB7D5; border-color: #9DB7D5; }}
QPushButton#Danger {{ color: {DANGER}; border-color: #FDA29B; }}
QPushButton#Tile, QPushButton#TileWarn, QPushButton#TileDanger, QPushButton#TileZero {{
    text-align: left; padding: 10px 14px; border-radius: 8px; font-size: 10pt; min-height: 52px; }}
QPushButton#Tile {{ background: white; border: 1px solid #E4E7EC; color: #101828; }}
QPushButton#TileWarn {{ background: #FFFAEB; border: 1px solid #FEC84B; color: {WARNING}; font-weight: 600; }}
QPushButton#TileDanger {{ background: #FEF3F2; border: 1px solid #FDA29B; color: {DANGER}; font-weight: 600; }}
QPushButton#TileZero {{ background: white; border: 1px solid #E4E7EC; color: #98A2B3; }}
QPushButton#Tile:hover, QPushButton#TileWarn:hover, QPushButton#TileDanger:hover, QPushButton#TileZero:hover {{
    border-color: {ACCENT}; }}
QPushButton#Tile:focus, QPushButton#TileWarn:focus, QPushButton#TileDanger:focus, QPushButton#TileZero:focus {{
    border: 2px solid {ACCENT}; }}
QLineEdit, QComboBox, QDateEdit, QSpinBox, QDoubleSpinBox, QPlainTextEdit, QTextEdit {{
    background: white; border: 1px solid #D0D5DD; border-radius: 6px; padding: 5px 8px; }}
QLineEdit:focus, QComboBox:focus, QDateEdit:focus, QPlainTextEdit:focus {{ border: 1px solid {ACCENT}; }}
QLineEdit[invalid="true"] {{ border: 1px solid {DANGER}; background: #FFFBFA; }}
QLineEdit#GlobalSearch {{ padding: 6px 10px; min-width: 360px; }}
QTableView, QTreeView, QTreeWidget, QListWidget {{ background: white; border: 1px solid #E4E7EC; border-radius: 6px;
    gridline-color: #EEF0F3; selection-background-color: #D6E6F7; selection-color: #101828;
    alternate-background-color: #FAFBFC; }}
QHeaderView::section {{ background: #F2F4F7; border: none; border-bottom: 1px solid #E4E7EC;
    border-right: 1px solid #E4E7EC; padding: 6px; font-weight: 600; color: #344054; }}
QTabWidget::pane {{ border: 1px solid #E4E7EC; border-radius: 6px; background: white; top: -1px; }}
QTabBar::tab {{ padding: 7px 16px; border: 1px solid transparent; color: #475467; }}
QTabBar::tab:selected {{ background: white; border: 1px solid #E4E7EC; border-bottom-color: white;
    border-top-left-radius: 6px; border-top-right-radius: 6px; color: #101828; font-weight: 600; }}
QGroupBox {{ border: 1px solid #E4E7EC; border-radius: 8px; margin-top: 14px; background: white; padding: 10px; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px; color: #344054; font-weight: 600; }}
QTreeWidget::item {{ padding: 4px 2px; }}
QListWidget::item {{ padding: 6px 8px; }}
QStatusBar {{ background: white; border-top: 1px solid #E4E7EC; color: {MUTED}; }}
QPushButton#Chip {{ background: #EFF8FF; border: 1px solid #B2DDFF; border-radius: 12px; padding: 3px 10px;
    color: #175CD3; }}
QPushButton#Chip:hover {{ background: #D1E9FF; }}
QPushButton#ChipFixed {{ background: #F2F4F7; border: 1px solid #D0D5DD; border-radius: 12px; padding: 3px 10px;
    color: #344054; }}
QPushButton#Link {{ background: transparent; border: none; color: {ACCENT}; padding: 3px 6px; }}
QPushButton#Link:hover {{ text-decoration: underline; }}
QFrame#SelectionBar {{ background: #EFF8FF; border: 1px solid #B2DDFF; border-radius: 6px; }}
QLabel#Count {{ color: #344054; font-weight: 600; }}
QFrame#ProjectBar {{ background: #EFF8FF; border: 1px solid #B2DDFF; border-radius: 6px; }}
QLabel#ProjectTitle {{ color: #175CD3; font-weight: 600; }}
QLabel#ProjectFigure {{ color: #344054; }}
QLabel#AlertGroup {{ color: {MUTED}; font-size: 8pt; font-weight: 600; }}
QPushButton#AlertCritical, QPushButton#AlertAction, QPushButton#AlertData {{ text-align: left; padding: 5px 12px;
    border-radius: 6px; }}
QPushButton#AlertCritical {{ background: #FEF3F2; border: 1px solid #FDA29B; color: {DANGER}; font-weight: 600; }}
QPushButton#AlertAction {{ background: #FFFAEB; border: 1px solid #FEC84B; color: {WARNING}; font-weight: 600; }}
QPushButton#AlertData {{ background: white; border: 1px solid #E4E7EC; color: #475467; }}
QPushButton#AlertCritical:hover, QPushButton#AlertAction:hover, QPushButton#AlertData:hover {{
    border-color: {ACCENT}; }}
QPushButton#AlertCritical:focus, QPushButton#AlertAction:focus, QPushButton#AlertData:focus {{
    border: 2px solid {ACCENT}; }}
QFrame#TodayBar {{ background: white; border: 1px solid #E4E7EC; border-radius: 6px; }}
QPushButton#TodayItem, QPushButton#TodayZero {{ background: transparent; border: none; padding: 3px 8px; }}
QPushButton#TodayItem {{ color: #101828; font-weight: 600; }}
QPushButton#TodayZero {{ color: #98A2B3; }}
QPushButton#TodayItem:hover, QPushButton#TodayZero:hover {{ color: {ACCENT}; text-decoration: underline; }}
QPushButton#QuickAction {{ background: {ACCENT}; border: 1px solid {ACCENT}; color: white; font-weight: 600;
    padding: 6px 12px; min-width: 96px; }}
QPushButton#QuickAction:hover {{ background: #285F99; }}
QToolButton#UserMenu {{ background: transparent; border: 1px solid transparent; border-radius: 6px;
    padding: 6px 10px; color: #344054; }}
QToolButton#UserMenu:hover {{ background: #F2F4F7; border-color: #D0D5DD; }}
QToolButton#UserMenu::menu-indicator {{ image: none; width: 0; }}
"""


def apply_theme(qt_app) -> None:
    """Always use the light theme, whatever Windows is set to.

    Windows dark mode hands Qt a dark palette (white text). Our style sheet sets
    light backgrounds, so without this the text would be white on white.
    """
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QColor, QPalette

    try:
        qt_app.styleHints().setColorScheme(Qt.ColorScheme.Light)  # Qt 6.8+
    except AttributeError:
        pass
    qt_app.setStyle("Fusion")  # same look on every Windows version, ignores the system palette
    pal = QPalette()
    R = QPalette.ColorRole
    for role, colour in ((R.Window, WINDOW_BG), (R.WindowText, TEXT), (R.Base, "#FFFFFF"),
                         (R.AlternateBase, "#FAFBFC"), (R.Text, TEXT), (R.Button, "#FFFFFF"),
                         (R.ButtonText, "#344054"), (R.ToolTipBase, "#FFFFFF"), (R.ToolTipText, TEXT),
                         (R.Highlight, "#D6E6F7"), (R.HighlightedText, TEXT), (R.PlaceholderText, "#98A2B3"),
                         (R.Link, ACCENT), (R.BrightText, "#FFFFFF")):
        pal.setColor(role, QColor(colour))
    for role in (R.WindowText, R.Text, R.ButtonText):
        pal.setColor(QPalette.ColorGroup.Disabled, role, QColor("#98A2B3"))
    qt_app.setPalette(pal)
    qt_app.setStyleSheet(QSS)
