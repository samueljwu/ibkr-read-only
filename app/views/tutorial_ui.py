"""A lightweight spotlight for the main-window navigation tour."""

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from app.views.ui_theme import COLORS


class TutorialOverlay(QWidget):
    def __init__(self, parent, back, forward, exit_tour):
        super().__init__(parent)
        self.setObjectName("tutorialOverlay")
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFocusPolicy(Qt.StrongFocus)
        self.target_rect = QRect()
        self.theme = "light"

        self.card = QWidget(self)
        self.card.setObjectName("tutorialCard")
        layout = QVBoxLayout(self.card)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)
        self.progress = QLabel()
        self.progress.setObjectName("tutorialProgress")
        self.title = QLabel()
        self.title.setObjectName("tutorialTitle")
        self.title.setWordWrap(True)
        self.body = QLabel()
        self.body.setObjectName("tutorialBody")
        self.body.setWordWrap(True)
        layout.addWidget(self.progress)
        layout.addWidget(self.title)
        layout.addWidget(self.body)
        buttons = QHBoxLayout()
        self.exit_button = QPushButton("Exit tour")
        self.exit_button.setAccessibleName("Exit tutorial")
        self.exit_button.clicked.connect(exit_tour)
        self.back_button = QPushButton("Back")
        self.back_button.clicked.connect(back)
        self.next_button = QPushButton("Next")
        self.next_button.clicked.connect(forward)
        buttons.addWidget(self.exit_button)
        buttons.addStretch()
        buttons.addWidget(self.back_button)
        buttons.addWidget(self.next_button)
        layout.addLayout(buttons)
        self.hide()

    def show_step(self, number, total, title, body, target_rect, theme):
        self.progress.setText(f"Step {number} of {total}")
        self.title.setText(title)
        self.body.setText(body)
        self.back_button.setEnabled(number > 1)
        self.next_button.setText("Finish" if number == total else "Next")
        self.target_rect = target_rect
        self.theme = theme
        self._place_card()
        self.update()
        self.show()
        self.raise_()
        self.setFocus()

    def update_target(self, target_rect):
        self.target_rect = target_rect
        self._place_card()
        self.update()

    def _place_card(self):
        width = min(420, max(280, self.width() - 32))
        self.card.setFixedWidth(width)
        self.card.adjustSize()
        height = self.card.sizeHint().height()
        target = self.target_rect
        x = max(16, min(target.center().x() - width // 2, self.width() - width - 16))
        below = target.bottom() + 22
        above = target.top() - height - 22
        if below + height <= self.height() - 16:
            y = below
        elif above >= 16:
            y = above
        else:
            y = max(16, self.height() - height - 16)
        self.card.move(x, y)

    def paintEvent(self, event):
        painter = QPainter(self)
        shade = QColor(0, 0, 0, 145)
        if not self.target_rect.isValid():
            painter.fillRect(self.rect(), shade)
            return
        target = self.target_rect.adjusted(-5, -5, 5, 5).intersected(self.rect())
        painter.fillRect(QRect(0, 0, self.width(), target.top()), shade)
        painter.fillRect(QRect(0, target.bottom() + 1, self.width(),
                               self.height() - target.bottom() - 1), shade)
        painter.fillRect(QRect(0, target.top(), target.left(), target.height()), shade)
        painter.fillRect(QRect(target.right() + 1, target.top(),
                               self.width() - target.right() - 1, target.height()), shade)
        painter.setPen(QPen(QColor(COLORS[self.theme]["accent"]), 3))
        painter.drawRoundedRect(target, 5, 5)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.exit_button.click()
        elif event.key() in (Qt.Key_Return, Qt.Key_Enter):
            self.next_button.click()
        else:
            super().keyPressEvent(event)
