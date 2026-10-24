"""Visible navigation for the main dashboard pages."""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QAbstractItemView, QListWidget


class MainNavigation(QListWidget):
    orderChanged = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("mainNavigation")
        self.setAccessibleName("Main navigation")
        self.setSelectionMode(QAbstractItemView.SingleSelection)
        self.setDragDropMode(QAbstractItemView.InternalMove)
        self.setDefaultDropAction(Qt.MoveAction)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setToolTip("Drag sections to reorder")

    def dropEvent(self, event):
        super().dropEvent(event)
        if event.isAccepted():
            self.orderChanged.emit()
