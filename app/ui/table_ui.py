"""Shared table selection, copying, numeric display, and sorting for desktop tabs."""

import json
from decimal import Decimal

from PySide6.QtCore import QEvent, QItemSelection, QItemSelectionModel, Qt
from PySide6.QtGui import QActionGroup, QKeySequence
from PySide6.QtWidgets import (QAbstractItemView, QAbstractSpinBox, QApplication, QComboBox,
                               QInputDialog, QLineEdit, QMenu, QTableWidget, QTableWidgetItem)

from app.ui.clipboard_ui import add_table_copy_actions, copy_table_selection


def fit_option_name_column(table, column, names, db, setting_key):
    """Widen a name column once when descriptive options first appear."""
    names = [str(name) for name in names if name and " · " in str(name)
             and (" Call " in str(name) or " Put " in str(name))]
    if not names or db.setting(setting_key) == "1":
        return
    width = min(440, max(table.fontMetrics().horizontalAdvance(name) + 28 for name in names))
    table.setColumnWidth(column, max(table.columnWidth(column), width))
    db.set_setting(setting_key, "1")


def handle_table_shortcut(view, event):
    """Apply the same row, column, and copy shortcuts in every data table."""
    if event.modifiers() == Qt.ShiftModifier and event.key() == Qt.Key_Space:
        anchor = view.currentIndex()
        if anchor.isValid():
            view.selection_axis = "row"
            full_row = QItemSelection(view.model().index(anchor.row(), 0),
                                      view.model().index(anchor.row(), view.model().columnCount() - 1))
            view.selectionModel().select(full_row, QItemSelectionModel.ClearAndSelect)
            view.selectionModel().setCurrentIndex(anchor, QItemSelectionModel.NoUpdate)
        return True
    if event.modifiers() == Qt.ControlModifier and event.key() == Qt.Key_Space:
        anchor = view.currentIndex()
        if anchor.isValid():
            view.selection_axis = "column"
            model = view.model()
            top = model.index(0, anchor.column())
            bottom = model.index(model.rowCount() - 1, anchor.column())
            view.selectionModel().select(QItemSelection(top, bottom), QItemSelectionModel.ClearAndSelect)
            view.selectionModel().setCurrentIndex(anchor, QItemSelectionModel.NoUpdate)
        return True
    if event.matches(QKeySequence.Copy):
        copy_table_selection(view)
        return True
    return False


class SortItem(QTableWidgetItem):
    """Sort a displayed value by its underlying number or date when available."""

    def __init__(self, label, sort_value=None):
        super().__init__(label)
        self.sort_value = sort_value

    def __lt__(self, other):
        if isinstance(other, SortItem) and self.sort_value is not None and other.sort_value is not None:
            return self.sort_value < other.sort_value
        return self.text().casefold() < other.text().casefold()


class ReportTable(QTableWidget):
    """Consistent read-only table behavior; embedded editors still keep focus."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setSelectionBehavior(QAbstractItemView.SelectItems)
        self.setAlternatingRowColors(False)
        self.setShowGrid(False)
        self.setWordWrap(False)
        self.verticalHeader().setDefaultSectionSize(25)
        self.horizontalHeader().setSectionsMovable(True)
        self.selection_axis = "cell"
        self._number_formats = {}
        self._column_decimals = {}
        self._settings_db = None
        self._settings_key = None
        self._allow_header_sort = True
        self.horizontalHeader().viewport().installEventFilter(self)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._cell_menu)

    def _build_cell_menu(self):
        menu = add_table_copy_actions(QMenu(self), self)
        if isinstance(self, InputReportTable):
            paste = menu.addAction("Paste")
            paste.setEnabled(self._paste_editor() is not None)
            paste.triggered.connect(self.paste_current_cell)
        return menu

    def _cell_menu(self, point):
        index = self.indexAt(point)
        if index.isValid() and index not in self.selectedIndexes():
            self.setCurrentIndex(index)
        self._build_cell_menu().exec(self.viewport().mapToGlobal(point))

    def eventFilter(self, watched, event):
        if (watched is self.horizontalHeader().viewport() and event.type() == QEvent.MouseButtonPress
                and event.button() == Qt.LeftButton and event.modifiers() == Qt.ControlModifier):
            column = self.horizontalHeader().logicalIndexAt(event.position().toPoint())
            if column >= 0 and self.rowCount():
                anchor = self.model().index(0, column)
                bottom = self.model().index(self.rowCount() - 1, column)
                self.selection_axis = "column"
                self.selectionModel().select(QItemSelection(anchor, bottom), QItemSelectionModel.ClearAndSelect)
                self.selectionModel().setCurrentIndex(anchor, QItemSelectionModel.NoUpdate)
            return True
        return super().eventFilter(watched, event)

    def setItem(self, row, column, item):
        item.setFlags(item.flags() & ~Qt.ItemIsEditable)
        super().setItem(row, column, item)

    def configure_numbers(self, db, setting_key, formats, *, allow_sort=True):
        """Enable numeric sorting and the common decimal menu for selected columns.

        formats maps column numbers to (default decimal places, unit), where unit is
        plain, usd, or percent. Existing rows can be updated using numeric_item().
        """
        self._settings_db, self._settings_key = db, setting_key
        self._number_formats = dict(formats)
        self._allow_header_sort = allow_sort
        try:
            saved = json.loads(db.setting(setting_key) or "{}")
        except (TypeError, ValueError):
            saved = {}
        if not isinstance(saved, dict):
            saved = {}
        self._column_decimals = {
            int(key): value for key, value in saved.items()
            if str(key).isdigit() and int(key) in formats and isinstance(value, int)
            and not isinstance(value, bool) and 0 <= value <= 30
        }
        header = self.horizontalHeader()
        header.setContextMenuPolicy(Qt.CustomContextMenu)
        header.customContextMenuRequested.connect(self._header_menu)

    def decimals_for(self, column):
        return self._column_decimals.get(column, self._number_formats[column][0])

    def format_number(self, column, value):
        if value is None:
            return "—"
        _, unit = self._number_formats[column]
        places = self.decimals_for(column)
        number = Decimal(str(value))
        if unit == "percent":
            return f"{number:+,.{places}f}%"
        prefix = "$" if unit == "usd" else ""
        return f"{prefix}{number:,.{places}f}"

    def numeric_item(self, column, value):
        item = SortItem(self.format_number(column, value), value)
        item.raw_value = value
        item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
        return item

    def set_column_decimals(self, column, places):
        if column not in self._number_formats or not isinstance(places, int) or isinstance(places, bool) or not 0 <= places <= 30:
            raise ValueError("Choose 0–30 decimal places for a numeric column")
        if places == self._number_formats[column][0]:
            self._column_decimals.pop(column, None)
        else:
            self._column_decimals[column] = places
        if self._settings_db is not None:
            self._settings_db.set_setting(self._settings_key, json.dumps(self._column_decimals))
        sorting = self.isSortingEnabled()
        if sorting:
            self.setSortingEnabled(False)
        for row in range(self.rowCount()):
            item = self.item(row, column)
            if item is not None and hasattr(item, "raw_value"):
                item.setText(self.format_number(column, item.raw_value))
        if sorting:
            self.setSortingEnabled(True)

    def _build_header_menu(self, column):
        menu = QMenu(self)
        if self._allow_header_sort:
            menu.addAction("Sort ascending", lambda: self.sortItems(column, Qt.AscendingOrder))
            menu.addAction("Sort descending", lambda: self.sortItems(column, Qt.DescendingOrder))
        if column in self._number_formats:
            decimals_menu = QMenu("Decimal places", menu)
            menu.addMenu(decimals_menu)
            group = QActionGroup(decimals_menu)
            group.setExclusive(True)
            for places in range(4):
                action = decimals_menu.addAction(f"{places} decimal place{'s' if places != 1 else ''}")
                action.setCheckable(True)
                action.setChecked(self.decimals_for(column) == places)
                group.addAction(action)
                action.triggered.connect(lambda checked=False, count=places:
                                         self.set_column_decimals(column, count))
            custom = decimals_menu.addAction("Custom…")
            custom.setCheckable(True)
            custom.setChecked(self.decimals_for(column) > 3)
            group.addAction(custom)
            custom.triggered.connect(lambda: self._choose_column_decimals(column))
        menu.addSeparator()
        menu.addAction("Hide column", lambda: self.hideColumn(column))
        menu.addAction("Show all columns", self._show_all_columns)
        return menu

    def _show_all_columns(self):
        for column in range(self.columnCount()):
            self.showColumn(column)

    def _choose_column_decimals(self, column):
        places, accepted = QInputDialog.getInt(self, "Custom decimal places",
                                                "Decimal places (0–30):", self.decimals_for(column), 0, 30)
        if accepted:
            self.set_column_decimals(column, places)

    def _header_menu(self, point):
        header = self.horizontalHeader()
        column = header.logicalIndexAt(point)
        if column >= 0:
            self._build_header_menu(column).exec(header.mapToGlobal(point))

    def keyPressEvent(self, event):
        if handle_table_shortcut(self, event):
            event.accept()
            return
        super().keyPressEvent(event)


class InputReportTable(ReportTable):
    """Shared keyboard behavior for tables with embedded input widgets."""

    DIRECTIONS = {Qt.Key_Up: (-1, 0), Qt.Key_Down: (1, 0),
                  Qt.Key_Left: (0, -1), Qt.Key_Right: (0, 1)}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._input_bindings = {}

    def clear_input_bindings(self):
        self._input_bindings.clear()

    def register_input(self, editor, row_key, column, commit=None):
        if isinstance(editor, QComboBox):
            baseline = editor.currentText() if editor.isEditable() else editor.currentIndex()
        elif isinstance(editor, QLineEdit):
            baseline = editor.text()
        else:
            baseline = editor.value()
        binding = (editor, row_key, column, baseline, commit)
        self._input_bindings[editor] = binding
        editor.installEventFilter(self)
        if isinstance(editor, QAbstractSpinBox):
            line_edit = editor.lineEdit()
            self._input_bindings[line_edit] = binding
            line_edit.installEventFilter(self)
        elif isinstance(editor, QComboBox) and editor.isEditable():
            line_edit = editor.lineEdit()
            self._input_bindings[line_edit] = binding
            line_edit.installEventFilter(self)

    def row_for_key(self, row_key):
        for row in range(self.rowCount()):
            item = self.item(row, 0)
            if item is not None and item.data(Qt.UserRole) == row_key:
                return row
        return -1

    def select_cell(self, row, column, *, focus=False):
        if focus:
            self.setFocus()
        index = self.model().index(row, column)
        selection = self.selectionModel()
        selection.blockSignals(True)
        selection.setCurrentIndex(index, QItemSelectionModel.ClearAndSelect)
        selection.blockSignals(False)
        self.selection_axis = "cell"
        self.scrollTo(index, QAbstractItemView.EnsureVisible)
        self.viewport().update()

    def move_cell(self, row, column, key):
        if not self.rowCount():
            return
        change_row, change_column = self.DIRECTIONS[key]
        row = max(0, min(self.rowCount() - 1, row + change_row))
        if change_column:
            header = self.horizontalHeader()
            candidate_visual = header.visualIndex(column) + change_column
            while 0 <= candidate_visual < header.count():
                candidate = header.logicalIndex(candidate_visual)
                if not self.isColumnHidden(candidate):
                    column = candidate
                    break
                candidate_visual += change_column
        self.select_cell(row, column, focus=True)

    def _cell_editor(self, row, column):
        widget = self.cellWidget(row, column)
        if isinstance(widget, (QAbstractSpinBox, QComboBox, QLineEdit)):
            return widget
        if widget is not None:
            return (widget.findChild(QAbstractSpinBox) or widget.findChild(QLineEdit) or
                    widget.findChild(QComboBox))
        return None

    def keyPressEvent(self, event):
        if event.matches(QKeySequence.Paste):
            self.paste_current_cell()
            event.accept()
            return
        if event.modifiers() == Qt.NoModifier and event.key() in self.DIRECTIONS:
            self.move_cell(self.currentRow() if self.currentRow() >= 0 else 0,
                           self.currentColumn() if self.currentColumn() >= 0 else 0, event.key())
            event.accept()
            return
        if event.key() in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_F2):
            editor = self._cell_editor(self.currentRow(), self.currentColumn())
            if editor is not None:
                editor.setFocus()
                if isinstance(editor, QAbstractSpinBox):
                    editor.selectAll()
                event.accept()
                return
        super().keyPressEvent(event)

    def _paste_editor(self):
        editor = self._cell_editor(self.currentRow(), self.currentColumn())
        if editor is None or not editor.isEnabled():
            return None
        if isinstance(editor, QAbstractSpinBox):
            return editor.lineEdit() if not editor.isReadOnly() else None
        if isinstance(editor, QComboBox):
            return editor.lineEdit() if editor.isEditable() and not editor.lineEdit().isReadOnly() else None
        return editor if not editor.isReadOnly() else None

    def paste_current_cell(self):
        editor = self._paste_editor()
        text = QApplication.clipboard().text()
        if editor is not None and not any(separator in text for separator in ("\t", "\n", "\r")):
            editor.setFocus()
            editor.selectAll()
            editor.paste()

    def eventFilter(self, watched, event):
        binding = self._input_bindings.get(watched)
        if binding is None or event.type() != QEvent.KeyPress:
            return super().eventFilter(watched, event)
        key = event.key()
        is_enter = key in (Qt.Key_Return, Qt.Key_Enter)
        if (key not in (*self.DIRECTIONS, Qt.Key_Escape, Qt.Key_Return, Qt.Key_Enter) or
            event.modifiers() not in ((Qt.NoModifier, Qt.KeypadModifier) if is_enter else (Qt.NoModifier,))):
            return super().eventFilter(watched, event)
        editor, row_key, column, baseline, commit = binding
        editor.blockSignals(True)
        if watched is not editor:
            watched.blockSignals(True)
        if key == Qt.Key_Escape:
            if isinstance(editor, QComboBox):
                if editor.isEditable():
                    editor.setCurrentText(baseline)
                else:
                    editor.setCurrentIndex(baseline)
            elif isinstance(editor, QLineEdit):
                editor.setText(baseline)
            else:
                editor.setValue(baseline)
        elif isinstance(editor, QAbstractSpinBox):
            editor.interpretText()
        self.setFocus()
        if watched is not editor:
            watched.blockSignals(False)
        editor.blockSignals(False)
        if key != Qt.Key_Escape and commit is not None:
            commit()
        row = self.row_for_key(row_key)
        if row >= 0:
            if key == Qt.Key_Escape:
                self.select_cell(row, column, focus=True)
            else:
                self.move_cell(row, column, Qt.Key_Down if is_enter else key)
        event.accept()
        return True
