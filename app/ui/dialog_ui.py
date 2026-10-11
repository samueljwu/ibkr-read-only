"""Shared window controls for application pop-ups."""

from PySide6.QtCore import QEvent, QObject, QTimer, Qt
from PySide6.QtWidgets import QApplication, QDialog, QInputDialog, QLayout, QMessageBox


def _enable_window_controls(dialog):
    if isinstance(dialog, QMessageBox):
        dialog.setOption(QMessageBox.DontUseNativeDialog, True)
    dialog.setWindowFlag(Qt.MSWindowsFixedSizeDialogHint, False)
    dialog.setWindowFlag(Qt.WindowMaximizeButtonHint, True)
    dialog.setSizeGripEnabled(True)


class DialogResizeFilter(QObject):
    def eventFilter(self, watched, event):
        if isinstance(watched, QDialog):
            if event.type() == QEvent.Polish:
                _enable_window_controls(watched)
            if (isinstance(watched, (QMessageBox, QInputDialog))
                    and event.type() in (QEvent.Show, QEvent.LayoutRequest, QEvent.Resize)
                    and not watched.property('_resize_pending')):
                watched.setProperty('_resize_pending', True)
                # Qt applies fixed bounds during show and when details change.
                # Release them after its event handler has finished.
                QTimer.singleShot(0, watched, lambda: self._release_bounds(watched))
            if isinstance(watched, QMessageBox) and event.type() == QEvent.LayoutRequest:
                # Its default handler reapplies a fixed size and cancels
                # maximizing. The queued callback activates the layout instead.
                return True
        return super().eventFilter(watched, event)

    @staticmethod
    def _release_bounds(dialog):
        dialog.setProperty('_resize_pending', False)
        layout = dialog.layout()
        # QMessageBox manages its own layout. A minimum-size constraint would
        # repeatedly trigger its updateSize() and snap the window back.
        constraint = (QLayout.SetNoConstraint if isinstance(dialog, QMessageBox)
                      else QLayout.SetMinimumSize)
        if layout is not None and layout.sizeConstraint() != constraint:
            layout.setSizeConstraint(constraint)
        if layout is not None:
            layout.activate()
        if dialog.maximumWidth() != 16777215 or dialog.maximumHeight() != 16777215:
            dialog.setMaximumSize(16777215, 16777215)
        minimum = dialog.minimumSizeHint()
        if minimum.isValid() and dialog.minimumSize() != minimum:
            dialog.setMinimumSize(minimum)


def install_dialog_resize_support():
    app = QApplication.instance()
    if not hasattr(app, '_dialog_resize_filter'):
        app._dialog_resize_filter = DialogResizeFilter(app)
        app.installEventFilter(app._dialog_resize_filter)


class ResizableDialog(QDialog):
    """Expose edge/corner resizing and the native maximize control."""

    def __init__(self, parent=None):
        super().__init__(parent)
        install_dialog_resize_support()
        _enable_window_controls(self)
