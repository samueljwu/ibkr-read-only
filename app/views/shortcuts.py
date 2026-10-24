"""Shortcut choices and labels shared by the app and its guide."""

import sys

from PySide6.QtGui import QKeySequence


NEXT_TAB = QKeySequence("Ctrl+PgDown")
PREVIOUS_TAB = QKeySequence("Ctrl+PgUp")


def shortcut(default: str, mac: str | None = None) -> QKeySequence:
    return QKeySequence(mac if sys.platform == "darwin" and mac else default)


def native_text(sequence: QKeySequence) -> str:
    return sequence.toString(QKeySequence.NativeText)
