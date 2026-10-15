"""Export a read-only portfolio workbook."""

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill


def export_workbook(path, portfolio, trades, lots, columns):
    book = Workbook()
    for index, (name, rows) in enumerate((("Portfolio", portfolio), ("Trades", trades), ("Lots", lots))):
        sheet = book.active if index == 0 else book.create_sheet()
        sheet.title = name
        specs = columns[name]
        sheet.append([label for _, label, _ in specs])
        for cell in sheet[1]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="234269")
        for row in rows:
            sheet.append([row.get(key) for key, _, _ in specs])
            for cell, (_, _, kind) in zip(sheet[sheet.max_row], specs):
                if isinstance(cell.value, str):
                    cell.data_type = "s"
                if kind == "pct":
                    cell.number_format = "0.00%"
                elif kind in ("money", "price", "number", "ratio"):
                    cell.number_format = "#,##0.00;[Red](#,##0.00)"
        sheet.freeze_panes = "B2"
        sheet.auto_filter.ref = sheet.dimensions
        for col in sheet.columns:
            letter = col[0].column_letter
            sheet.column_dimensions[letter].width = min(28, max(12, max(len(str(c.value or "")) for c in col[:100]) + 2))
    book.save(path)
