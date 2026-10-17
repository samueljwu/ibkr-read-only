"""Small shared widget factories and layout/persistence helpers for desktop tabs.

These are intentionally plain functions (no wrapper classes) so call sites stay
simple: build a widget, then apply a role via objectName and shared stylesheet
rules in ui_theme.py.
"""

import json

from PySide6.QtCore import QByteArray, Qt
from PySide6.QtWidgets import (QComboBox, QDialogButtonBox, QHBoxLayout, QLabel,
                               QPushButton, QSizePolicy, QSplitter, QVBoxLayout, QWidget)

from app.views.ui_theme import (CARD_MARGINS, DIALOG_MARGINS, DIALOG_SPACING,
                                 PAGE_MARGINS, PAGE_SPACING, SUBPAGE_MARGINS,
                                 style_toolbar_dropdown)


def apply_layout(layout, kind):
    """Apply the shared margins/spacing for a layout role.

    kind is one of "page", "subpage", "card", "dialog". "subpage" and "card"
    only set margins (no shared spacing constant is defined for them).
    """
    if kind == "page":
        layout.setContentsMargins(*PAGE_MARGINS)
        layout.setSpacing(PAGE_SPACING)
    elif kind == "subpage":
        layout.setContentsMargins(*SUBPAGE_MARGINS)
    elif kind == "card":
        layout.setContentsMargins(*CARD_MARGINS)
    elif kind == "dialog":
        layout.setContentsMargins(*DIALOG_MARGINS)
        layout.setSpacing(DIALOG_SPACING)
    else:
        raise ValueError(f"Unknown layout kind: {kind!r}")
    return layout


def section_title(text):
    label = QLabel(text)
    label.setObjectName("sectionTitle")
    return label


def hint(text):
    label = QLabel(text)
    label.setObjectName("hint")
    label.setWordWrap(True)
    return label


def field_label(text, buddy=None):
    label = QLabel(text)
    label.setObjectName("fieldLabel")
    if buddy is not None:
        label.setBuddy(buddy)
    return label


def card():
    """Create a themed card container; returns (widget, layout)."""
    widget = QWidget()
    widget.setObjectName("card")
    layout = QVBoxLayout(widget)
    apply_layout(layout, "card")
    return widget, layout


def button(text, slot=None, variant="secondary"):
    """Create a QPushButton styled by variant.

    "primary" gets no objectName (the bare QPushButton rule is the primary
    style); any other variant ("secondary", "danger", "link", ...) is set as
    the button's objectName so the matching stylesheet rule applies.
    """
    widget = QPushButton(text)
    if variant != "primary":
        widget.setObjectName(variant)
    if slot is not None:
        widget.clicked.connect(slot)
    return widget


def dialog_buttons(dialog, standard, primary=None):
    """Build or style a QDialogButtonBox for a dialog.

    standard may be an existing QDialogButtonBox instance, or a
    QDialogButtonBox.StandardButtons flags value used to construct one.
    Close/Cancel buttons are styled as secondary; Save/Ok/Apply keep the
    default (primary) styling. If primary is given, it is connected to the
    box's accepted signal.
    """
    box = standard if isinstance(standard, QDialogButtonBox) else QDialogButtonBox(standard)
    for role in (QDialogButtonBox.Close, QDialogButtonBox.Cancel):
        button_widget = box.button(role)
        if button_widget is not None:
            button_widget.setObjectName("secondary")
    if primary is not None:
        box.accepted.connect(primary)
    return box


def present(dialog):
    dialog.show()
    dialog.raise_()
    dialog.activateWindow()


def enable_label_copy(root):
    """Let users select displayed text without adding labels to tab navigation."""
    for label in root.findChildren(QLabel):
        label.setTextInteractionFlags(label.textInteractionFlags() | Qt.TextSelectableByMouse)


def repolish(widget):
    """Force a widget to re-evaluate stylesheet rules after a property change."""
    widget.style().unpolish(widget)
    widget.style().polish(widget)


def remember_splitter(splitter, db, key):
    """Persist and restore a QSplitter's sizes under a single settings key.

    Restores saved sizes immediately if present (ignoring invalid data), and
    saves sizes to db on every splitterMoved. Returns the connected callback so
    callers can disconnect it when changing the settings key.
    """
    saved = db.setting(key)
    if saved:
        try:
            sizes = json.loads(saved)
        except (TypeError, ValueError):
            sizes = None
        if isinstance(sizes, list) and sizes and all(isinstance(size, int) for size in sizes):
            splitter.setSizes(sizes)

    def _save(*_):
        db.set_setting(key, json.dumps(splitter.sizes()))

    splitter.splitterMoved.connect(_save)
    return _save


def _chart_table_state_key(key, horizontal):
    return f"{key}_horizontal" if horizontal else key


def save_chart_table_split_state(db, splitter, key):
    state_key = _chart_table_state_key(key, splitter.orientation() == Qt.Horizontal)
    db.set_setting(state_key, bytes(splitter.saveState().toBase64()).decode("ascii"))


def set_chart_table_layout(db, splitter, key, horizontal):
    if (splitter.orientation() == Qt.Horizontal) == horizontal:
        return
    save_chart_table_split_state(db, splitter, key)
    splitter.blockSignals(True)
    try:
        splitter.setOrientation(Qt.Horizontal if horizontal else Qt.Vertical)
        saved = db.setting(_chart_table_state_key(key, horizontal))
        if not saved or not splitter.restoreState(QByteArray.fromBase64(saved.encode("ascii"))):
            splitter.setSizes((600, 400))
    finally:
        splitter.blockSignals(False)
    db.set_setting(f"{key}_orientation", "horizontal" if horizontal else "vertical")


def chart_table_layout(layout, db, key, chart, table, *, info_label=None, navigator=None):
    """Add the shared chart/table orientation control and persistent splitter."""
    horizontal = db.setting(f"{key}_orientation") == "horizontal"
    controls = QHBoxLayout()
    controls.setSpacing(8)
    if info_label is not None:
        controls.addWidget(info_label, 1)
    controls.addStretch()
    controls.addWidget(QLabel("Chart and table"))
    layout_combo = style_toolbar_dropdown(QComboBox())
    layout_combo.addItems(("Top and bottom", "Side by side"))
    layout_combo.setCurrentIndex(1 if horizontal else 0)
    controls.addWidget(layout_combo)
    layout.addLayout(controls)

    splitter = QSplitter(Qt.Horizontal if horizontal else Qt.Vertical)
    splitter.setChildrenCollapsible(False)
    splitter.setHandleWidth(11)
    splitter.setMinimumWidth(0)
    splitter.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)
    chart_panel = QWidget()
    chart_panel.setMinimumWidth(0)
    chart_layout = QVBoxLayout(chart_panel)
    chart_layout.setContentsMargins(0, 0, 0, 0)
    chart_layout.setSpacing(2)
    chart_layout.addWidget(chart, 1)
    if navigator is not None:
        chart_layout.addWidget(navigator)
    splitter.addWidget(chart_panel)
    splitter.addWidget(table)
    splitter.setStretchFactor(0, 3)
    splitter.setStretchFactor(1, 2)
    saved = db.setting(_chart_table_state_key(key, horizontal))
    if not saved or not splitter.restoreState(QByteArray.fromBase64(saved.encode("ascii"))):
        splitter.setSizes((600, 400))
    splitter.splitterMoved.connect(lambda *_: save_chart_table_split_state(db, splitter, key))
    layout_combo.currentIndexChanged.connect(
        lambda index: set_chart_table_layout(db, splitter, key, index == 1))
    layout.addWidget(splitter, 1)
    splitter.handle(1).setToolTip("Drag to resize chart and table")
    return splitter, layout_combo


def persist_tab_order(tab_widget, db, key, ids, remember_active=False):
    """Make a QTabWidget's tab order (and optionally active tab) persistent.

    ids is the ordered list of stable string ids for the tabs currently in
    tab_widget, in their current left-to-right order. The saved JSON shape is
    {"version": 1, "order": [...ids...], "active": <id, or legacy int index>}.
    """
    bar = tab_widget.tabBar()
    bar.setMovable(True)
    bar.setToolTip("Drag tabs to reorder")
    for index, tab_id in enumerate(ids):
        bar.setTabData(index, tab_id)

    def _current_order():
        return [bar.tabData(index) for index in range(bar.count())]

    def _save(*_):
        state = {"version": 1, "order": _current_order(),
                 "active": bar.tabData(tab_widget.currentIndex())}
        db.set_setting(key, json.dumps(state))

    try:
        saved = json.loads(db.setting(key) or "null")
    except (TypeError, ValueError):
        saved = None
    if isinstance(saved, dict) and saved.get("version") == 1 and isinstance(saved.get("order"), list):
        order = [tab_id for tab_id in saved["order"] if tab_id in ids]
        order += [tab_id for tab_id in ids if tab_id not in order]
        for destination, tab_id in enumerate(order):
            source = next((index for index in range(bar.count()) if bar.tabData(index) == tab_id), -1)
            if source >= 0 and source != destination:
                bar.moveTab(source, destination)
        if remember_active:
            active = saved.get("active")
            if isinstance(active, int) and not isinstance(active, bool) and 0 <= active < len(ids):
                active = ids[active]
            target = next((index for index in range(bar.count()) if bar.tabData(index) == active), -1)
            if target >= 0:
                tab_widget.setCurrentIndex(target)

    bar.tabMoved.connect(_save)
    if remember_active:
        tab_widget.currentChanged.connect(_save)
