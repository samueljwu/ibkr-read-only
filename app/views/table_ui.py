"""Shared table selection, copying, numeric display, and sorting for desktop tabs."""

import json
from decimal import Decimal

from PySide6.QtCore import QByteArray, QEvent, QItemSelection, QItemSelectionModel, QObject, QTimer, Qt
from PySide6.QtGui import QActionGroup, QKeySequence
from PySide6.QtWidgets import (QAbstractButton, QAbstractItemView, QAbstractSlider,
                               QAbstractSpinBox, QApplication, QComboBox, QInputDialog,
                               QLineEdit, QMenu, QMenuBar, QPlainTextEdit, QTabBar,
                               QTableView, QTableWidget, QTableWidgetItem, QTextEdit, QWidget)


def display_number(value):
    return f"{float(value):,.2f}"


def money(value, symbol=True):
    """Format a dollar amount: "$1,234.56", or "—" for None. symbol=False omits "$"."""
    if value is None:
        return "—"
    prefix = "$" if symbol else ""
    return f"{prefix}{Decimal(str(value)):,.2f}"


def percent(value):
    """Format a signed percent: "+1.23%", or "—" for None."""
    if value is None:
        return "—"
    return f"{Decimal(str(value)):+,.2f}%"


def build_header_menu(parent, header, column, *, sort, decimals, labels):
    """Build the shared column-header context menu for a QHeaderView.

    parent: QWidget parent for the returned QMenu.
    header: the QHeaderView the menu was invoked on (used for hide/show and
        header.count()).
    column: the logical column index the menu was invoked on.
    sort: optional callable sort(column, ascending: bool), or None to omit the
        sort actions (e.g. tables that don't support interactive sorting).
    decimals: optional (get_fn, set_fn) tuple for decimal-place support on
        this column, or None if the column isn't numeric. get_fn(column) ->
        current decimal count; set_fn(column, places) applies a new count.
        Custom… prompts via QInputDialog using parent as the dialog parent.
    labels: list of all column header labels, used for the Columns submenu.

    Returns the built QMenu; the caller is responsible for calling .exec(...).
    """
    menu = QMenu(parent)
    if sort is not None:
        menu.addAction("Sort ascending", lambda: sort(column, True))
        menu.addAction("Sort descending", lambda: sort(column, False))
    if decimals is not None:
        get_fn, set_fn = decimals
        decimals_menu = QMenu("Decimal places", menu)
        menu.addMenu(decimals_menu)
        group = QActionGroup(decimals_menu)
        group.setExclusive(True)
        current = get_fn(column)
        for places in range(4):
            action = decimals_menu.addAction(f"{places} decimal place{'s' if places != 1 else ''}")
            action.setCheckable(True)
            action.setChecked(current == places)
            group.addAction(action)
            action.triggered.connect(lambda checked=False, count=places: set_fn(column, count))
        custom = decimals_menu.addAction("Custom…")
        custom.setCheckable(True)
        custom.setChecked(current > 3)
        group.addAction(custom)

        def _choose_custom():
            places, accepted = QInputDialog.getInt(
                parent, "Custom decimal places", "Decimal places (0–30):", current, 0, 30)
            if accepted:
                set_fn(column, places)

        custom.triggered.connect(_choose_custom)
    menu.addSeparator()
    menu.addAction("Hide column", lambda: header.hideSection(column))
    columns_menu = QMenu("Columns", menu)
    menu.addMenu(columns_menu)
    for index, label in enumerate(labels):
        action = columns_menu.addAction(label)
        action.setCheckable(True)
        action.setChecked(not header.isSectionHidden(index))
        action.toggled.connect(lambda checked, number=index: header.setSectionHidden(number, not checked))
    menu.addAction("Show all columns",
                   lambda: [header.setSectionHidden(index, False) for index in range(header.count())])
    return menu


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


def copy_table_selection(view):
    """Copy selected cells as tab-separated text, preserving empty gaps."""
    indexes = view.selectedIndexes()
    if not indexes:
        return
    top = min(index.row() for index in indexes)
    left = min(index.column() for index in indexes)
    bottom = max(index.row() for index in indexes)
    right = max(index.column() for index in indexes)
    selected = {(index.row(), index.column()) for index in indexes}
    lines = []
    for row in range(top, bottom + 1):
        values = []
        for column in range(left, right + 1):
            value = view.model().data(view.model().index(row, column), Qt.EditRole)
            values.append(str(value) if (row, column) in selected and value is not None else "")
        lines.append("\t".join(values))
    QApplication.clipboard().setText("\n".join(lines))


class TableClickAwayFilter(QObject):
    """Clear visible table selections when blank space in their window is clicked."""

    def __init__(self, root):
        super().__init__(root)
        self.root = root

    def eventFilter(self, watched, event):
        if (event.type() != QEvent.MouseButtonPress or event.button() != Qt.LeftButton
                or not isinstance(watched, QWidget)):
            return False
        window = watched.window()
        owner = window
        while owner is not None and owner is not self.root:
            owner = owner.parent()
        if owner is None:
            return False
        widget = watched
        while widget is not None and widget is not window:
            if isinstance(widget, QTableView):
                return False
            widget = widget.parentWidget()
        if isinstance(watched, (QAbstractButton, QAbstractSlider, QAbstractSpinBox,
                                QComboBox, QLineEdit, QMenu, QMenuBar, QPlainTextEdit,
                                QTabBar, QTextEdit)):
            return False
        for table in window.findChildren(QTableView):
            if table.window() is window and table.isVisible() and table.selectionModel().hasSelection():
                table.selectionModel().clear()
        return False


class SortItem(QTableWidgetItem):
    """Sort a displayed value by its underlying number or date when available."""

    def __init__(self, label, sort_value=None):
        super().__init__(label)
        self.sort_value = sort_value

    def __lt__(self, other):
        if isinstance(other, SortItem):
            mine, theirs = self.sort_value, other.sort_value
            if mine is None and theirs is None:
                return self.text().casefold() < other.text().casefold()
            if mine is None or theirs is None:
                # A missing value sorts after every known value instead of
                # being compared to it as text.
                return theirs is None
            return mine < theirs
        return self.text().casefold() < other.text().casefold()


class ReportTable(QTableWidget):
    """Consistent read-only table behavior; embedded editors still keep focus."""

    def row_for_key(self, row_key):
        for row in range(self.rowCount()):
            item = self.item(row, 0)
            if item is not None and item.data(Qt.UserRole) == row_key:
                return row
        return -1

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setSelectionBehavior(QAbstractItemView.SelectItems)
        self.setAlternatingRowColors(False)
        self.setShowGrid(False)
        self.setWordWrap(False)
        self.verticalHeader().setDefaultSectionSize(25)
        self.verticalHeader().setVisible(False)
        self.horizontalHeader().setSectionsMovable(True)
        self.selection_axis = "cell"
        self._number_formats = {}
        self._column_decimals = {}
        self._settings_db = None
        self._settings_key = None
        self._allow_header_sort = True
        self._layout_db = None
        self._layout_key = None
        self._layout_timer = None
        self.horizontalHeader().viewport().installEventFilter(self)

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
        plain, usd, percent, or ratio_percent. Existing rows can be updated using numeric_item().
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
        if unit == "ratio_percent":
            return f"{number:+,.{places}%}"
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

    def view_state(self):
        """Return this table's header geometry, sort, and decimal places as JSON-safe data.

        Keys match the saved-view JSON shape already used by analytics/exposure/
        cash tabs: header (base64 str), sort_column (int), sort_order ("asc"/
        "desc"), decimals (dict of str(column) -> int).
        """
        header = self.horizontalHeader()
        return {
            "header": bytes(header.saveState().toBase64()).decode("ascii"),
            "sort_column": header.sortIndicatorSection(),
            "sort_order": "desc" if header.sortIndicatorOrder() == Qt.DescendingOrder else "asc",
            "decimals": {str(column): self.decimals_for(column) for column in self._number_formats},
        }

    def apply_view_state(self, state):
        """Restore header geometry, sort, and decimal places saved by view_state()."""
        if not isinstance(state, dict):
            return
        header_state = state.get("header")
        if isinstance(header_state, str):
            self.horizontalHeader().restoreState(
                QByteArray.fromBase64(header_state.encode("ascii", "ignore")))
        decimals = state.get("decimals")
        if isinstance(decimals, dict):
            for column in self._number_formats:
                places = decimals.get(str(column))
                if isinstance(places, int) and not isinstance(places, bool) and 0 <= places <= 30:
                    self.set_column_decimals(column, places)
        column = state.get("sort_column")
        if isinstance(column, int) and not isinstance(column, bool) and 0 <= column < self.columnCount():
            self.sortItems(column, Qt.DescendingOrder if state.get("sort_order") == "desc"
                           else Qt.AscendingOrder)

    def remember_layout(self, db, key):
        """Load a saved view_state() immediately, then persist future header/sort changes.

        Changes are debounced through a single timer so a burst of column
        resizes/moves/sorts writes only once, ~400ms after the last change.
        """
        try:
            saved = json.loads(db.setting(key) or "null")
        except (TypeError, ValueError):
            saved = None
        if isinstance(saved, dict):
            self.apply_view_state(saved)
        self._layout_db, self._layout_key = db, key
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.setInterval(400)
        timer.timeout.connect(self._save_layout)
        self._layout_timer = timer
        header = self.horizontalHeader()
        header.sectionMoved.connect(self._schedule_layout)
        header.sectionResized.connect(self._schedule_layout)
        header.sortIndicatorChanged.connect(self._schedule_layout)

    def _schedule_layout(self, *_):
        if self._layout_db is not None:
            self._layout_timer.start()

    def _save_layout(self):
        if self._layout_db is not None:
            self._layout_db.set_setting(self._layout_key, json.dumps(self.view_state()))

    def finish_layout_persistence(self):
        """Save a pending layout and release the database before it closes."""
        if self._layout_timer is not None:
            pending = self._layout_timer.isActive()
            self._layout_timer.stop()
            if pending:
                self._save_layout()
        self._layout_db = self._layout_key = None

    def closeEvent(self, event):
        self.finish_layout_persistence()
        super().closeEvent(event)

    def refill(self, rows, build):
        """Rebuild all rows while preserving sort order and (best-effort) selection.

        build(row_data) -> list[QTableWidgetItem] populates one row; rows are
        the new data in order. Sorting is disabled during the rebuild and
        restored afterward; selection is restored by matching the previously
        selected items' text.
        """
        header = self.horizontalHeader()
        sort_column, sort_order = header.sortIndicatorSection(), header.sortIndicatorOrder()
        selected_text = {item.text() for item in self.selectedItems()}
        sorting = self.isSortingEnabled()
        if sorting:
            self.setSortingEnabled(False)
        self.clearContents()
        self.setRowCount(len(rows))
        for row_index, row_data in enumerate(rows):
            for column_index, item in enumerate(build(row_data)):
                self.setItem(row_index, column_index, item)
        if sorting:
            self.setSortingEnabled(True)
        if 0 <= sort_column < self.columnCount():
            self.sortItems(sort_column, sort_order)
        if selected_text:
            self.clearSelection()
            for row_index in range(self.rowCount()):
                item = self.item(row_index, 0)
                if item is not None and item.text() in selected_text:
                    self.selectRow(row_index)

    def _build_header_menu(self, column):
        header = self.horizontalHeader()
        sort = ((lambda col, ascending:
                 self.sortItems(col, Qt.AscendingOrder if ascending else Qt.DescendingOrder))
                if self._allow_header_sort else None)
        decimals = ((self.decimals_for, self.set_column_decimals)
                    if column in self._number_formats else None)
        labels = [self.horizontalHeaderItem(index).text() if self.horizontalHeaderItem(index) else ""
                  for index in range(self.columnCount())]
        return build_header_menu(self, header, column, sort=sort, decimals=decimals, labels=labels)

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

    def _copy_menu(self, point):
        index = self.indexAt(point)
        if index.isValid() and index not in self.selectedIndexes():
            self.setCurrentIndex(index)
        menu = QMenu(self)
        action = menu.addAction("Copy", lambda: copy_table_selection(self))
        action.setEnabled(bool(self.selectedIndexes()))
        return menu

    def contextMenuEvent(self, event):
        self._copy_menu(event.pos()).exec(event.globalPos())


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
