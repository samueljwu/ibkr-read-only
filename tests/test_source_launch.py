from pathlib import Path
from types import SimpleNamespace

import main


def test_main_passes_explicit_database_to_window(monkeypatch, tmp_path):
    calls = []

    class App:
        def __init__(self, args):
            pass

        def setApplicationVersion(self, value):
            pass

        def setApplicationName(self, value):
            pass

        def setApplicationDisplayName(self, value):
            pass

        def setWindowIcon(self, value):
            pass

        def exec(self):
            return 0

    class Window:
        def __init__(self, database_path, *, on_manage_portfolios):
            calls.append(database_path)
            self.db = SimpleNamespace(path=database_path, recovery_code=None)
            self.destroyed = SimpleNamespace(connect=lambda callback: None)

        def setAttribute(self, *args):
            pass

        def show(self):
            pass

    monkeypatch.setattr(main, "QApplication", App)
    monkeypatch.setattr(main, "install_app_font", lambda app: None)
    monkeypatch.setattr(main, "QIcon", lambda path: None)
    monkeypatch.setattr(main, "MainWindow", Window)
    monkeypatch.setattr(main, "DatabaseSelectionWindow", lambda *args: None)
    monkeypatch.setattr(main.Database, "select_database", lambda path: None)
    chosen = tmp_path / "fresh run" / "portfolio.sqlite3"
    assert main.main(["--database", str(chosen)]) == 0
    assert calls == [chosen]
