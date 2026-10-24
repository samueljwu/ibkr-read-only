"""Shared metric presentation and responsive summary rows for analytics views."""

from PySide6.QtCore import QEvent, QPoint, Qt
from PySide6.QtWidgets import (QComboBox, QGridLayout, QHBoxLayout, QLabel, QPushButton, QSizePolicy,
                               QToolButton, QToolTip, QVBoxLayout, QWidget)

from app.views.ui_theme import CARD_MARGINS


def _metric_labels(widget, caption, *, info=False):
    layout = QVBoxLayout(widget)
    layout.setContentsMargins(8, 4, 8, 4)
    layout.setSpacing(2)
    widget.caption = QLabel(caption)
    widget.caption.setObjectName("metricLabel")
    widget.caption.setWordWrap(False)
    widget.caption.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
    widget.caption.setMinimumHeight(20)
    widget.value = QLabel("—")
    widget.value.setObjectName("metricValue")
    if info:
        widget.info = MetricInfoButton(caption)
        heading = QHBoxLayout()
        heading.setSpacing(5)
        heading.addWidget(widget.caption)
        heading.addWidget(widget.info)
        heading.addStretch()
        layout.addLayout(heading)
    else:
        layout.addWidget(widget.caption)
    layout.addWidget(widget.value)
    layout.addStretch()


class MetricValue(QWidget):
    """A summary figure without an associated selection action."""

    def __init__(self, caption, *, info=False):
        super().__init__()
        _metric_labels(self, caption, info=info)


class MetricButton(QPushButton):
    """A keyboard-accessible metric whose label and value select its chart."""

    def __init__(self, caption):
        super().__init__()
        self.setObjectName("metricButton")
        self.setCheckable(True)
        self.setAccessibleName(f"Chart {caption}")
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        _metric_labels(self, caption)
        for label in (self.caption, self.value):
            label.setAttribute(Qt.WA_TransparentForMouseEvents)

    def sizeHint(self):
        return self.layout().sizeHint()


class MetricInfoButton(QToolButton):
    """Show secondary figure details on hover, focus, or click."""

    def __init__(self, caption, parent=None):
        super().__init__(parent)
        self.setObjectName("metricInfoButton")
        self.setText("i")
        self.setAccessibleName(f"{caption} details")
        self.setFixedSize(20, 20)
        self.clicked.connect(self._show_detail)
        self.hide()

    def set_detail(self, detail):
        self.setToolTip(detail)
        self.setAccessibleDescription(detail)
        self.setVisible(bool(detail))

    def _show_detail(self):
        if self.toolTip():
            QToolTip.showText(self.mapToGlobal(QPoint(0, self.height())), self.toolTip(), self)


class MetricDropdown(QComboBox):
    """Keep long metric names fully visible in the control and its popup."""

    def __init__(self):
        super().__init__()
        self.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        self.setMinimumWidth(160)

    def showPopup(self):
        if self.count():
            text_width = max(self.view().fontMetrics().horizontalAdvance(self.itemText(index))
                             for index in range(self.count()))
            self.view().setMinimumWidth(max(self.width(), text_width + 32))
        super().showPopup()


class MetricSummary(QWidget):
    """Wrap analytics metrics when their text no longer fits in one row."""

    def __init__(self, *, framed=True, divider=False):
        super().__init__()
        self.setObjectName("analyticsSummary" if framed else "metricSummaryRow")
        self.setAttribute(Qt.WA_StyledBackground)
        self.setProperty("divider", divider)
        self.grid = QGridLayout(self)
        self.grid.setContentsMargins(*(CARD_MARGINS if framed else (0, 0, 0, 12 if divider else 0)))
        self.grid.setSpacing(12 if framed else 18)
        self._blocks = []
        self._columns = 0

    def set_blocks(self, blocks):
        self._blocks = blocks
        self._reflow()

    def _reflow(self):
        if not self._blocks:
            return
        margins = self.grid.contentsMargins()
        available = max(1, self.width() - margins.left() - margins.right())
        min_width = 180
        spacing = self.grid.horizontalSpacing()
        fit = max(1, (available + spacing) // (min_width + spacing))
        columns = min(len(self._blocks), fit)
        if len(self._blocks) == 6:
            # Give a wrapped row room for labels and pair related figures at narrow widths.
            if columns >= 6:
                columns = 6
            elif available >= 3 * 300 + 2 * spacing:
                columns = 3
            else:
                columns = min(columns, 2)
        if columns != self._columns:
            for block in self._blocks:
                if isinstance(block, QWidget):
                    self.grid.removeWidget(block)
                else:
                    self.grid.removeItem(block)
            for column in range(max(self._columns, columns)):
                self.grid.setColumnStretch(column, 0)
            for index, block in enumerate(self._blocks):
                if isinstance(block, QWidget):
                    self.grid.addWidget(block, index // columns, index % columns)
                else:
                    self.grid.addLayout(block, index // columns, index % columns)
            for column in range(columns):
                self.grid.setColumnStretch(column, 1)
            self._columns = columns
        self.grid.activate()
        minimum = self.grid.sizeHint().height()
        if minimum != self.minimumHeight():
            self.setMinimumHeight(minimum)
        self.updateGeometry()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._reflow()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() in (QEvent.FontChange, QEvent.StyleChange):
            self._reflow()
