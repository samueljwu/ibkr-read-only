"""Small shared widget factories and layout/persistence helpers for desktop tabs.

These are intentionally plain functions (no wrapper classes) so call sites stay
simple: build a widget, then apply a role via objectName and shared stylesheet
rules in ui_theme.py.
"""

import json

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialogButtonBox, QLabel, QPushButton, QVBoxLayout, QWidget

from app.views.ui_theme import (CARD_MARGINS, DIALOG_MARGINS, DIALOG_SPACING,
                                 PAGE_MARGINS, PAGE_SPACING, SUBPAGE_MARGINS)


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
