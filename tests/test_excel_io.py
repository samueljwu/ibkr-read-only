from openpyxl import load_workbook

from app.storage.excel_io import export_workbook
from app.ui import COLUMNS


def test_exported_workbook_contains_synced_trades(tmp_path):
    path = tmp_path / "portfolio.xlsx"
    export_workbook(path,
                    [{"display_ticker": "QQQ US Equity", "ib_symbol": "QQQ", "position_qty": 12}],
                    [{"trade_id": "IBKR:101", "trade_date": "2026-01-02",
                      "display_ticker": "QQQ US Equity", "source": "IBKR Flex",
                      "side": "BUY", "quantity": 12, "price": 100}], [], COLUMNS)
    book = load_workbook(path, read_only=True, data_only=True)
    try:
        headers = [cell.value for cell in book["Trades"][1]]
        values = [cell.value for cell in book["Trades"][2]]
        row = dict(zip(headers, values))
        assert row["Reference"] == "IBKR:101"
        assert row["Origin"] == "IBKR Flex"
        assert row["Quantity"] == 12
    finally:
        book.close()


def test_export_keeps_formula_like_user_text_as_text(tmp_path):
    path = tmp_path / "safe.xlsx"
    export_workbook(path, [{"display_ticker": "=1+1"}], [], [], COLUMNS)
    book = load_workbook(path)
    try:
        headings = [cell.value for cell in book["Portfolio"][1]]
        cell = book["Portfolio"].cell(2, headings.index("Instrument") + 1)
        assert cell.value == "=1+1"
        assert cell.data_type == "s"
    finally:
        book.close()
