"""Editor for named, underlying-based exposure categories."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QDialog,
                               QDialogButtonBox, QHBoxLayout, QHeaderView,
                               QInputDialog, QLabel, QMessageBox, QPushButton,
                               QTableWidgetItem, QVBoxLayout)

from app.finance.exposure import (add_custom_set, custom_labels, custom_sets,
                          delete_custom_set, exposure_snapshot, rename_custom_set,
                          set_custom_label)
from app.views.table_ui import InputReportTable


class CustomSetDialog(QDialog):
    """Edit one category per underlying; options share that assignment."""

    def __init__(self, panel, set_name):
        super().__init__(panel)
        self.panel = panel
        self.db = panel.db
        self.portfolio_id = panel.portfolio_id
        self.setWindowTitle("Custom exposure categories")
        self.resize(670, 480)
        layout = QVBoxLayout(self)
        header = QHBoxLayout()
        header.addWidget(QLabel("Set"))
        self.set_combo = QComboBox()
        self.set_combo.addItems(custom_sets(self.db, self.portfolio_id))
        self.set_combo.setCurrentText(set_name)
        self.current_set = self.set_combo.currentText()
        header.addWidget(self.set_combo, 1)
        for caption, handler in (("New set", self._new), ("Rename", self._rename),
                                 ("Delete", self._delete)):
            button = QPushButton(caption)
            button.setObjectName("secondary")
            button.clicked.connect(handler)
            header.addWidget(button)
        layout.addLayout(header)
        help_label = QLabel("Choose or type a category beside each underlying. Its options use the same category.")
        help_label.setObjectName("hint")
        help_label.setWordWrap(True)
        layout.addWidget(help_label)
        self.table = InputReportTable(0, 2)
        self.table.setHorizontalHeaderLabels(("Underlying", "Category"))
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        layout.addWidget(self.table, 1)
        self.empty_status = QLabel("No resolved underlyings yet. Sync positions with IBKR, then return here.")
        self.empty_status.setObjectName("hint")
        layout.addWidget(self.empty_status)
        apply_row = QHBoxLayout()
        apply_row.addWidget(QLabel("For selected rows"))
        self.apply_category = QComboBox()
        self.apply_category.setEditable(True)
        apply_row.addWidget(self.apply_category, 1)
        apply_button = QPushButton("Apply category")
        apply_button.setObjectName("secondary")
        apply_button.clicked.connect(self._apply_selected)
        apply_row.addWidget(apply_button)
        layout.addLayout(apply_row)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Close)
        self.save_button = buttons.button(QDialogButtonBox.Save)
        self.save_button.setText("Save changes")
        self.save_button.clicked.connect(self._save_and_close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.set_combo.currentTextChanged.connect(self._set_changed)
        self._load()

    def _underlyings(self):
        snapshot = exposure_snapshot(self.db, self.panel.service, self.portfolio_id)
        names = {}
        for row in snapshot["positions"]:
            if row["under_con_id"]:
                name = row.get("under_symbol") or row["symbol"].split(" ")[0]
                names.setdefault(row["under_con_id"], name)
                if row["type"] == "STK":
                    names[row["under_con_id"]] = row["symbol"]
        return sorted(names.items(), key=lambda item: item[1].casefold())

    def _load(self):
        labels = custom_labels(self.db, self.portfolio_id, self.current_set) if self.current_set else {}
        categories = sorted(set(labels.values()), key=str.casefold)
        self.apply_category.clear()
        self.apply_category.addItems([""] + categories)
        self.apply_category.setCurrentText("")
        underlyings = self._underlyings()
        self.empty_status.setVisible(not underlyings)
        self.table.setEnabled(bool(self.current_set))
        self.apply_category.setEnabled(bool(self.current_set))
        self.save_button.setEnabled(bool(self.current_set))
        self.table.clear_input_bindings()
        self.table.setRowCount(len(underlyings))
        for index, (con_id, symbol) in enumerate(underlyings):
            item = QTableWidgetItem(symbol)
            item.setData(Qt.UserRole, con_id)
            item.setFlags(item.flags() & ~Qt.ItemIsEditable)
            self.table.setItem(index, 0, item)
            editor = QComboBox()
            editor.setEditable(True)
            editor.lineEdit().setPlaceholderText("Choose or type a category")
            editor.addItems([""] + categories)
            editor.setCurrentText(labels.get(con_id, ""))
            self.table.setCellWidget(index, 1, editor)
            self.table.register_input(editor, con_id, 1)

    def _save(self):
        if not self.current_set:
            return
        for row in range(self.table.rowCount()):
            con_id = self.table.item(row, 0).data(Qt.UserRole)
            label = self.table.cellWidget(row, 1).currentText().strip()
            set_custom_label(self.db, self.portfolio_id, self.current_set, con_id, label)
        self.panel._load_custom_sets(self.current_set)
        self.panel._group_changed()

    def _save_and_close(self):
        self._save()
        self.accept()

    def _set_changed(self, name):
        if name == self.current_set:
            return
        self._save()
        self.current_set = name
        self.panel._load_custom_sets(name)
        self.panel._group_changed()
        self._load()

    def _new(self):
        name, accepted = QInputDialog.getText(self, "New custom set", "Set name")
        name = name.strip()[:60]
        if not accepted or not name:
            return
        if name in custom_sets(self.db, self.portfolio_id):
            QMessageBox.information(self, "Set exists", "Choose a unique set name.")
            return
        self._save()
        add_custom_set(self.db, self.portfolio_id, name)
        self.set_combo.blockSignals(True)
        self.set_combo.addItem(name)
        self.set_combo.setCurrentText(name)
        self.set_combo.blockSignals(False)
        self.current_set = name
        self.panel._load_custom_sets(name)
        self.panel._group_changed()
        self._load()

    def _rename(self):
        if not self.current_set:
            return
        name, accepted = QInputDialog.getText(self, "Rename custom set", "Set name",
                                              text=self.current_set)
        if not accepted or name.strip() == self.current_set:
            return
        self._save()
        try:
            new_name = rename_custom_set(self.db, self.portfolio_id, self.current_set, name)
        except ValueError as exc:
            QMessageBox.information(self, "Invalid set name", str(exc))
            return
        index = self.set_combo.currentIndex()
        self.set_combo.blockSignals(True)
        self.set_combo.setItemText(index, new_name)
        self.set_combo.blockSignals(False)
        self.current_set = new_name
        self.panel._load_custom_sets(new_name)
        self.panel._group_changed()

    def _delete(self):
        if not self.current_set:
            return
        if QMessageBox.question(self, "Delete custom set",
                                f"Delete '{self.current_set}' and its assignments?") != QMessageBox.Yes:
            return
        delete_custom_set(self.db, self.portfolio_id, self.current_set)
        self.set_combo.blockSignals(True)
        self.set_combo.removeItem(self.set_combo.currentIndex())
        self.set_combo.blockSignals(False)
        self.current_set = self.set_combo.currentText()
        self._load()
        self.panel._load_custom_sets(self.current_set)
        self.panel._group_changed()

    def _apply_selected(self):
        label = self.apply_category.currentText().strip()
        for row in {item.row() for item in self.table.selectedItems()}:
            self.table.cellWidget(row, 1).setCurrentText(label)
