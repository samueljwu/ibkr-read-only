"""Shared clipboard behavior for informational text and report tables."""

import csv
from io import StringIO

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (QAbstractSpinBox, QApplication, QComboBox, QLabel,
                               QLineEdit, QMenu, QStatusBar, QTextEdit, QPlainTextEdit)


class ClipboardTextFilter(QObject):
    def eventFilter(self, watched, event):
        if (isinstance(watched, (QTextEdit, QPlainTextEdit)) and watched.isReadOnly()
                and event.type() in (QEvent.Polish, QEvent.Show)):
            watched.setCursorWidth(0)
        if isinstance(watched, QLabel) and event.type() in (QEvent.Polish, QEvent.Show):
            flags = watched.textInteractionFlags()
            # Keyboard text selection gives display labels a blinking caret.
            # Mouse selection still supports Ctrl+C and the native Copy menu.
            selectable = (flags | Qt.TextSelectableByMouse) & ~Qt.TextSelectableByKeyboard
            if flags != selectable:
                focus = watched.focusPolicy()
                watched.setTextInteractionFlags(selectable)
                watched.setFocusPolicy(focus if focus != Qt.NoFocus else Qt.ClickFocus)
        # Non-editable dropdowns and the status bar have useful text but no editor.
        plain = (isinstance(watched, QComboBox) and not watched.isEditable()) or isinstance(watched, QStatusBar)
        if plain:
            if event.type() == QEvent.KeyPress and event.matches(QKeySequence.Copy):
                QApplication.clipboard().setText(self._text(watched))
                return True
            if event.type() == QEvent.ContextMenu:
                menu = QMenu(watched)
                action = menu.addAction("Copy")
                action.setEnabled(bool(self._text(watched)))
                action.triggered.connect(lambda: QApplication.clipboard().setText(self._text(watched)))
                menu.exec(event.globalPos())
                return True
        return super().eventFilter(watched, event)

    @staticmethod
    def _text(widget):
        return widget.currentText() if isinstance(widget, QComboBox) else widget.currentMessage()


def install_clipboard_support():
    app = QApplication.instance()
    if not hasattr(app, "_clipboard_text_filter"):
        app._clipboard_text_filter = ClipboardTextFilter(app)
        app.installEventFilter(app._clipboard_text_filter)


def _cell_text(view, row, column):
    if hasattr(view, "_cell_editor"):
        editor = view._cell_editor(row, column)
        if isinstance(editor, QComboBox):
            return editor.currentText()
        if isinstance(editor, QAbstractSpinBox):
            return editor.text()
        if isinstance(editor, QLineEdit):
            return editor.text() if editor.echoMode() == QLineEdit.Normal else ""
    index = view.model().index(row, column)
    value = view.model().data(index, Qt.EditRole)
    if value is None:
        value = view.model().data(index, Qt.DisplayRole)
    return "" if value is None else str(value)


def copy_table_selection(view, *, with_headers=False):
    indexes = view.selectedIndexes()
    selected = {(index.row(), index.column()) for index in indexes
                if not view.isColumnHidden(index.column()) and not view.isRowHidden(index.row())}
    if not selected:
        return
    header = view.horizontalHeader()
    ordered = [header.logicalIndex(visual) for visual in range(header.count())
               if not view.isColumnHidden(header.logicalIndex(visual))]
    positions = [ordered.index(column) for _, column in selected]
    columns = ordered[min(positions):max(positions) + 1]
    first, last = min(row for row, _ in selected), max(row for row, _ in selected)
    rows = []
    if with_headers:
        rows.append([str(view.model().headerData(column, Qt.Horizontal, Qt.DisplayRole) or "")
                     for column in columns])
    for row in range(first, last + 1):
        if not view.isRowHidden(row):
            rows.append([_cell_text(view, row, column) if (row, column) in selected else ""
                         for column in columns])
    output = StringIO(newline="")
    writer = csv.writer(output, delimiter="\t", lineterminator="\r\n")
    writer.writerows(rows)
    QApplication.clipboard().setText(output.getvalue()[:-2].replace("\r\n", "\n"))


def add_table_copy_actions(menu, view):
    for title, headers in (("Copy", False), ("Copy with headers", True)):
        action = menu.addAction(title)
        action.setEnabled(bool(view.selectedIndexes()))
        action.triggered.connect(lambda checked=False, include=headers:
                                 copy_table_selection(view, with_headers=include))
    return menu
