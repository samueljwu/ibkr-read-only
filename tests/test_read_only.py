import ast
from pathlib import Path


# Every IBKR order or order-management entry point the app must never call.
FORBIDDEN = {
    "placeOrder", "cancelOrder", "reqOpenOrders", "reqAllOpenOrders",
    "reqGlobalCancel", "reqAutoOpenOrders", "reqCompletedOrders", "exerciseOptions",
}


def _sources():
    root = Path(__file__).resolve().parents[1]
    return (sorted(root.glob("*.py")) + sorted((root / "app").rglob("*.py")) +
            sorted((root / "scripts").rglob("*.py")))


def test_no_order_execution_calls():
    for path in _sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                assert node.func.attr not in FORBIDDEN, f"{path}: {node.func.attr}"
