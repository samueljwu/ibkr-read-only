"""Small SQLite store. Raw trades are never rewritten by lot matching."""

import sqlite3
import json
import math
import os
from uuid import uuid4
from contextlib import closing
from datetime import date, datetime
from pathlib import Path

from app.platform.paths import (default_data_dir, legacy_source_dir, local_config_path,
                                new_portfolio_dir, user_data_dir)
from app.finance.option_labels import option_label


DEFAULTS = {
    "provider": "Mock", "ib_host": "127.0.0.1", "ib_port": "7497",
    "ib_client_id": "17", "ib_account_id": "", "data_mode": "Live", "refresh_seconds": "30",
    "portfolio_nav_usd": "", "autosave": "1", "number_decimals": "2",
    "data_directory": "", "backup_directory": "",
    "flex_query_id": "", "analytics_flex_query_id": "", "cash_flex_query_id": "",
    "flex_auto_sync_enabled": "0",
}
POSITION_FIELDS = ("display_ticker", "ib_symbol", "security_type", "exchange", "primary_exchange",
                   "currency", "local_symbol", "expiry", "option_right", "strike", "trading_class",
                   "multiplier", "position_qty", "average_cost", "fx_to_usd", "notes")
TRADE_FIELDS = ("trade_id", "trade_date", "display_ticker", "ib_symbol", "side", "quantity",
                "price", "security_type", "multiplier", "currency", "fx_to_usd", "commission_usd", "notes")


class Database:
    def __init__(self, path=None):
        self.path = Path(path) if path is not None else self._default_path()
        if self.path.is_file():
            self._require_plain_sqlite(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=30000")
        self._init()

    @staticmethod
    def _require_plain_sqlite(path):
        with Path(path).open("rb") as source:
            header = source.read(16)
        if header and header != b"SQLite format 3\x00":
            raise ValueError(f"Portfolio database is encrypted or unsupported: {path}. "
                             "The current app needs a readable SQLite export.")

    @staticmethod
    def _configured_database(config_path):
        config = json.loads(config_path.read_text(encoding="utf-8"))
        directory = config.get("data_directory")
        if not directory:
            raise ValueError(f"No portfolio directory in {config_path}")
        database = Path(directory) / "portfolio.sqlite3"
        if not database.is_file():
            raise FileNotFoundError(f"Configured portfolio database is missing: {database}")
        return database

    @classmethod
    def _legacy_database(cls):
        base = user_data_dir()
        profile = default_data_dir().parent.name
        config_paths = ([base / "stable" / "data" / "local_config.json"] if profile == "prod" else [])
        config_paths += [base / "local_config.json", legacy_source_dir() / "local_config.json"]
        for config in config_paths:
            if config.is_file():
                candidate = cls._configured_database(config)
                if candidate.is_file():
                    return candidate
        candidates = ([base / "stable" / "data" / "portfolio.sqlite3"] if profile == "prod" else [])
        candidates += [base / "portfolio.sqlite3", legacy_source_dir() / "data" / "portfolio.sqlite3"]
        return next((candidate for candidate in candidates if candidate.is_file()), None)

    @classmethod
    def _default_path(cls):
        config_path = local_config_path()
        if config_path.is_file():
            return cls._configured_database(config_path)
        root = default_data_dir()
        folder = new_portfolio_dir(root)
        target = folder / "portfolio.sqlite3"
        try:
            source = cls._legacy_database()
            if source is not None:
                cls._require_plain_sqlite(source)
                with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as original:
                    with closing(sqlite3.connect(target)) as copied:
                        original.backup(copied)
                        if copied.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                            raise RuntimeError("Copied portfolio database failed integrity check")
                        with copied:
                            copied.execute("UPDATE settings SET value='' WHERE key='data_directory'")
            else:
                with closing(sqlite3.connect(target)):
                    pass
            config_path.parent.mkdir(parents=True, exist_ok=True)
            with config_path.open("x", encoding="utf-8") as config:
                json.dump({"data_directory": str(folder)}, config)
        except FileExistsError:
            target.unlink(missing_ok=True)
            folder.rmdir()
            return cls._configured_database(config_path)
        except Exception:
            target.unlink(missing_ok=True)
            folder.rmdir()
            raise
        return target

    def _init(self):
        self.conn.executescript("""
        CREATE TABLE IF NOT EXISTS portfolios (
          id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, nav_usd REAL CHECK(nav_usd >= 0 OR nav_usd IS NULL)
        );
        INSERT OR IGNORE INTO portfolios(id,name) VALUES (1,'Main');
        CREATE TABLE IF NOT EXISTS positions (
          id INTEGER PRIMARY KEY, portfolio_id INTEGER NOT NULL DEFAULT 1 REFERENCES portfolios(id),
          display_ticker TEXT NOT NULL DEFAULT '', ib_symbol TEXT NOT NULL DEFAULT '',
          security_type TEXT NOT NULL DEFAULT 'STK', exchange TEXT NOT NULL DEFAULT 'SMART',
          primary_exchange TEXT NOT NULL DEFAULT '', currency TEXT NOT NULL DEFAULT 'USD',
          local_symbol TEXT NOT NULL DEFAULT '', expiry TEXT NOT NULL DEFAULT '',
          option_right TEXT NOT NULL DEFAULT '', strike REAL, trading_class TEXT NOT NULL DEFAULT '',
          multiplier REAL DEFAULT 1 CHECK(multiplier > 0 OR multiplier IS NULL),
          position_qty REAL NOT NULL DEFAULT 0, average_cost REAL NOT NULL DEFAULT 0,
          fx_to_usd REAL NOT NULL DEFAULT 1, notes TEXT NOT NULL DEFAULT '',
          con_id INTEGER, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS trades (
          id INTEGER PRIMARY KEY, portfolio_id INTEGER NOT NULL DEFAULT 1 REFERENCES portfolios(id),
          position_id INTEGER REFERENCES positions(id),
          trade_id TEXT NOT NULL UNIQUE, trade_date TEXT NOT NULL DEFAULT '', executed_at TEXT NOT NULL DEFAULT '',
          display_ticker TEXT NOT NULL DEFAULT '', ib_symbol TEXT NOT NULL DEFAULT '',
          side TEXT NOT NULL DEFAULT 'BUY' CHECK(side IN ('BUY','SELL')),
          quantity REAL NOT NULL DEFAULT 0 CHECK(quantity >= 0), price REAL NOT NULL DEFAULT 0 CHECK(price >= 0),
          security_type TEXT NOT NULL DEFAULT 'STK', multiplier REAL DEFAULT 1 CHECK(multiplier > 0 OR multiplier IS NULL),
          currency TEXT NOT NULL DEFAULT 'USD', fx_to_usd REAL NOT NULL DEFAULT 1,
          commission_usd REAL NOT NULL DEFAULT 0, notes TEXT NOT NULL DEFAULT '',
          source TEXT NOT NULL DEFAULT 'manual',
          updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS trade_matches (
          match_id INTEGER PRIMARY KEY, buy_trade_id INTEGER NOT NULL REFERENCES trades(id) ON DELETE RESTRICT,
          sell_trade_id INTEGER NOT NULL REFERENCES trades(id) ON DELETE RESTRICT,
          matched_qty REAL NOT NULL CHECK(matched_qty > 0), origin TEXT NOT NULL DEFAULT 'manual',
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS contracts (
          position_id INTEGER PRIMARY KEY REFERENCES positions(id) ON DELETE CASCADE,
          con_id INTEGER NOT NULL, time_zone TEXT, resolved_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS historical_prices (
          con_id INTEGER NOT NULL, day TEXT NOT NULL, close REAL NOT NULL, volume REAL,
          PRIMARY KEY(con_id, day)
        );
        CREATE TABLE IF NOT EXISTS benchmark_prices (
          symbol TEXT NOT NULL, day TEXT NOT NULL, close REAL NOT NULL CHECK(close > 0),
          PRIMARY KEY(symbol, day)
        );
        CREATE TABLE IF NOT EXISTS market_quotes (
          portfolio_id INTEGER NOT NULL, account_id TEXT NOT NULL,
          position_id INTEGER NOT NULL REFERENCES positions(id) ON DELETE CASCADE,
          con_id INTEGER, price REAL NOT NULL CHECK(price > 0),
          source TEXT NOT NULL, as_of TEXT NOT NULL,
          PRIMARY KEY(portfolio_id, account_id, position_id)
        );
        CREATE TABLE IF NOT EXISTS exposure_facts (
          portfolio_id INTEGER NOT NULL REFERENCES portfolios(id),
          con_id INTEGER NOT NULL,
          sector TEXT NOT NULL DEFAULT '', category TEXT NOT NULL DEFAULT '',
          stock_type TEXT NOT NULL DEFAULT '',
          under_con_id INTEGER, under_symbol TEXT NOT NULL DEFAULT '',
          delta REAL, delta_mode TEXT NOT NULL DEFAULT '', delta_as_of TEXT NOT NULL DEFAULT '',
          underlying_price REAL, underlying_currency TEXT NOT NULL DEFAULT '',
          underlying_mode TEXT NOT NULL DEFAULT '', underlying_as_of TEXT NOT NULL DEFAULT '',
          details_as_of TEXT NOT NULL DEFAULT '',
          PRIMARY KEY(portfolio_id, con_id)
        );
        CREATE TABLE IF NOT EXISTS exposure_custom (
          portfolio_id INTEGER NOT NULL REFERENCES portfolios(id),
          set_name TEXT NOT NULL, con_id INTEGER NOT NULL, label TEXT NOT NULL,
          PRIMARY KEY(portfolio_id,set_name,con_id)
        );
        CREATE TABLE IF NOT EXISTS exposure_custom_sets (
          portfolio_id INTEGER NOT NULL REFERENCES portfolios(id),
          name TEXT NOT NULL, PRIMARY KEY(portfolio_id,name)
        );
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS app_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS broker_positions (
          portfolio_id INTEGER NOT NULL REFERENCES portfolios(id), con_id INTEGER NOT NULL,
          quantity REAL NOT NULL, symbol TEXT NOT NULL DEFAULT '', currency TEXT NOT NULL DEFAULT '',
          as_of TEXT NOT NULL, PRIMARY KEY(portfolio_id, con_id)
        );
        CREATE TABLE IF NOT EXISTS fx_rates (
          currency TEXT NOT NULL, day TEXT NOT NULL, usd_per_unit REAL NOT NULL CHECK(usd_per_unit > 0),
          source TEXT NOT NULL, PRIMARY KEY(currency, day)
        );
        CREATE TABLE IF NOT EXISTS account_values (
          portfolio_id INTEGER NOT NULL REFERENCES portfolios(id), metric TEXT NOT NULL,
          value REAL NOT NULL, currency TEXT NOT NULL, as_of TEXT NOT NULL,
          PRIMARY KEY(portfolio_id, metric)
        );
        CREATE TABLE IF NOT EXISTS account_history (
          portfolio_id INTEGER NOT NULL REFERENCES portfolios(id), day TEXT NOT NULL,
          nav_usd REAL NOT NULL CHECK(nav_usd >= 0), cash_usd REAL, daily_pnl_usd REAL,
          source TEXT NOT NULL, flow_covered INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY(portfolio_id, day)
        );
        CREATE TABLE IF NOT EXISTS account_monthly_history (
          portfolio_id INTEGER NOT NULL REFERENCES portfolios(id),
          account_id TEXT NOT NULL, month TEXT NOT NULL, period_from TEXT NOT NULL,
          period_to TEXT NOT NULL, nav_usd TEXT NOT NULL, return_pct TEXT NOT NULL,
          source_hash TEXT NOT NULL,
          PRIMARY KEY(portfolio_id, account_id, month)
        );
        CREATE TABLE IF NOT EXISTS legacy_account_performance (
          portfolio_id INTEGER NOT NULL REFERENCES portfolios(id), account_id TEXT NOT NULL,
          period_from TEXT NOT NULL, period_to TEXT NOT NULL,
          cumulative_return_pct TEXT NOT NULL, ending_nav_usd TEXT NOT NULL,
          measure TEXT NOT NULL,
          source_hash TEXT NOT NULL,
          PRIMARY KEY(portfolio_id, account_id, period_to)
        );
        CREATE TABLE IF NOT EXISTS legacy_position_openings (
          portfolio_id INTEGER NOT NULL REFERENCES portfolios(id), account_id TEXT NOT NULL,
          position_id INTEGER NOT NULL REFERENCES positions(id) ON DELETE CASCADE, as_of TEXT NOT NULL,
          symbol TEXT NOT NULL, currency TEXT NOT NULL,
          quantity TEXT NOT NULL, source_hash TEXT NOT NULL,
          PRIMARY KEY(portfolio_id, account_id, as_of, symbol, currency)
        );
        CREATE TABLE IF NOT EXISTS legacy_statement_sources (
          portfolio_id INTEGER NOT NULL REFERENCES portfolios(id), account_id TEXT NOT NULL,
          period_from TEXT NOT NULL, period_to TEXT NOT NULL, source_hash TEXT NOT NULL,
          ending_nav_usd TEXT NOT NULL, ending_cash_usd TEXT NOT NULL,
          dividend_accrual_usd TEXT NOT NULL, event_count INTEGER NOT NULL,
          PRIMARY KEY(portfolio_id, account_id, period_from, period_to)
        );
        CREATE TABLE IF NOT EXISTS legacy_statement_rows (
          portfolio_id INTEGER NOT NULL REFERENCES portfolios(id), account_id TEXT NOT NULL,
          period_from TEXT NOT NULL, period_to TEXT NOT NULL,
          section TEXT NOT NULL, row_number INTEGER NOT NULL, reported_day TEXT NOT NULL,
          payload_json TEXT NOT NULL,
          PRIMARY KEY(portfolio_id, account_id, period_from, period_to, section, row_number)
        );
        CREATE TABLE IF NOT EXISTS external_flows (
          portfolio_id INTEGER NOT NULL REFERENCES portfolios(id), source_id TEXT NOT NULL,
          day TEXT NOT NULL, amount_usd REAL NOT NULL, kind TEXT NOT NULL,
          PRIMARY KEY(portfolio_id, source_id)
        );
        CREATE TABLE IF NOT EXISTS cash_movements (
          portfolio_id INTEGER NOT NULL REFERENCES portfolios(id),
          account_id TEXT NOT NULL, source_key TEXT NOT NULL,
          reported_day TEXT NOT NULL, period_from TEXT NOT NULL, period_to TEXT NOT NULL,
          kind TEXT NOT NULL, currency TEXT NOT NULL, amount TEXT NOT NULL,
          description TEXT NOT NULL, source_section TEXT NOT NULL,
          con_id INTEGER, symbol TEXT NOT NULL DEFAULT '',
          transaction_id TEXT NOT NULL DEFAULT '', fx_to_usd TEXT,
          income_type TEXT NOT NULL DEFAULT '',
          source_hash TEXT NOT NULL, PRIMARY KEY(portfolio_id, account_id, source_key)
        );
        CREATE TABLE IF NOT EXISTS trade_fee_adjustments (
          portfolio_id INTEGER NOT NULL REFERENCES portfolios(id),
          account_id TEXT NOT NULL, source_key TEXT NOT NULL,
          trade_row_id INTEGER NOT NULL REFERENCES trades(id) ON DELETE RESTRICT,
          reported_day TEXT NOT NULL, refund_usd TEXT NOT NULL,
          description TEXT NOT NULL, source_hash TEXT NOT NULL,
          PRIMARY KEY(portfolio_id, account_id, source_key)
        );
        CREATE TABLE IF NOT EXISTS broker_executions (
          account_id TEXT NOT NULL, execution_key TEXT NOT NULL,
          trade_row_id INTEGER NOT NULL UNIQUE REFERENCES trades(id) ON DELETE CASCADE,
          latest_exec_id TEXT NOT NULL DEFAULT '', revision INTEGER NOT NULL DEFAULT 0,
          quality INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'pending',
          updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          PRIMARY KEY(account_id, execution_key)
        );
        CREATE TABLE IF NOT EXISTS broker_source_records (
          account_id TEXT NOT NULL, source TEXT NOT NULL, source_id TEXT NOT NULL,
          execution_key TEXT NOT NULL DEFAULT '', trade_row_id INTEGER REFERENCES trades(id) ON DELETE SET NULL,
          status TEXT NOT NULL, issue TEXT NOT NULL DEFAULT '', raw_json TEXT NOT NULL,
          seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          PRIMARY KEY(account_id, source, source_id)
        );
        CREATE TABLE IF NOT EXISTS broker_sync_windows (
          account_id TEXT NOT NULL, source TEXT NOT NULL, from_day TEXT NOT NULL, to_day TEXT NOT NULL,
          status TEXT NOT NULL, source_count INTEGER NOT NULL DEFAULT 0,
          imported_count INTEGER NOT NULL DEFAULT 0, issue_count INTEGER NOT NULL DEFAULT 0,
          completed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          PRIMARY KEY(account_id, source, from_day, to_day)
        );
        CREATE TABLE IF NOT EXISTS broker_sync_runs (
          id INTEGER PRIMARY KEY, account_id TEXT NOT NULL, source TEXT NOT NULL,
          started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, finished_at TEXT,
          status TEXT NOT NULL DEFAULT 'running', from_day TEXT, to_day TEXT,
          source_count INTEGER NOT NULL DEFAULT 0, inserted_count INTEGER NOT NULL DEFAULT 0,
          review_count INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT ''
        );
        """)
        added_positions = self._add_missing_columns("positions", {
            "portfolio_id": "INTEGER NOT NULL DEFAULT 1",
            "local_symbol": "TEXT NOT NULL DEFAULT ''", "expiry": "TEXT NOT NULL DEFAULT ''",
            "option_right": "TEXT NOT NULL DEFAULT ''", "strike": "REAL",
            "trading_class": "TEXT NOT NULL DEFAULT ''", "multiplier": "REAL DEFAULT 1"})
        added_trades = self._add_missing_columns("trades", {
            "portfolio_id": "INTEGER NOT NULL DEFAULT 1",
            "position_id": "INTEGER",
            "source": "TEXT NOT NULL DEFAULT 'manual'",
            "executed_at": "TEXT NOT NULL DEFAULT ''",
            "security_type": "TEXT NOT NULL DEFAULT 'STK'", "multiplier": "REAL DEFAULT 1"})
        self._add_missing_columns("trade_matches", {"origin": "TEXT NOT NULL DEFAULT 'manual'"})
        self._add_missing_columns("account_history", {"daily_pnl_usd": "REAL"})
        self._add_missing_columns("cash_movements", {
            "con_id": "INTEGER", "symbol": "TEXT NOT NULL DEFAULT ''",
            "transaction_id": "TEXT NOT NULL DEFAULT ''", "fx_to_usd": "TEXT",
            "income_type": "TEXT NOT NULL DEFAULT ''"})
        added_opening_fields = self._add_missing_columns("legacy_position_openings", {
            "symbol": "TEXT NOT NULL DEFAULT ''", "currency": "TEXT NOT NULL DEFAULT ''"})
        with self.conn:
            if "symbol" in added_opening_fields:
                self.conn.execute("""UPDATE legacy_position_openings SET symbol=COALESCE((
                    SELECT UPPER(TRIM(COALESCE(NULLIF(p.ib_symbol,''),p.display_ticker)))
                    FROM positions AS p WHERE p.id=legacy_position_openings.position_id), '')""")
            if "currency" in added_opening_fields:
                self.conn.execute("""UPDATE legacy_position_openings SET currency=COALESCE((
                    SELECT UPPER(TRIM(p.currency)) FROM positions AS p
                    WHERE p.id=legacy_position_openings.position_id), '')""")
            if "multiplier" in added_positions:
                self.conn.execute("UPDATE positions SET multiplier=NULL, con_id=NULL WHERE UPPER(security_type)!='STK'")
                self.conn.execute("DELETE FROM contracts WHERE position_id IN (SELECT id FROM positions WHERE UPPER(security_type)!='STK')")
            if "security_type" in added_trades:
                self.conn.execute("""UPDATE trades SET security_type=(
                    SELECT p.security_type FROM positions p
                    WHERE p.portfolio_id=trades.portfolio_id AND p.display_ticker=trades.display_ticker LIMIT 1)
                    WHERE EXISTS (SELECT 1 FROM positions p WHERE p.portfolio_id=trades.portfolio_id AND p.display_ticker=trades.display_ticker)""")
            if "multiplier" in added_trades:
                self.conn.execute("UPDATE trades SET multiplier=NULL WHERE UPPER(security_type)!='STK'")
            if "position_id" in added_trades:
                self.conn.execute("""UPDATE trades SET position_id=(
                    SELECT p.id FROM positions p WHERE p.portfolio_id=trades.portfolio_id
                    AND p.display_ticker=trades.display_ticker ORDER BY p.id LIMIT 1)""")
        with self.conn:
            self.conn.executemany("INSERT OR IGNORE INTO settings(key,value) VALUES (?,?)", DEFAULTS.items())
            legacy_cash_table = self.conn.execute("""SELECT 1 FROM sqlite_master
                WHERE type='table' AND name='cash_reconciliations'""").fetchone()
            if self.setting("cash_windows_migrated") != "1":
                # Preserve completed periods, including reports with no transactions,
                # when upgrading a database that used Cash Report balance records.
                if legacy_cash_table:
                    self.conn.execute("""INSERT OR IGNORE INTO broker_sync_windows
                        (account_id,source,from_day,to_day,status)
                        SELECT DISTINCT account_id,'flex_cash',period_from,period_to,'complete'
                        FROM cash_reconciliations""")
                self.conn.execute("""INSERT OR IGNORE INTO broker_sync_windows
                    (account_id,source,from_day,to_day,status)
                    SELECT DISTINCT account_id,'flex_cash',period_from,period_to,'complete'
                    FROM cash_movements""")
                self.conn.execute("""INSERT INTO settings(key,value)
                    VALUES ('cash_windows_migrated','1')
                    ON CONFLICT(key) DO UPDATE SET value='1'""")
            if legacy_cash_table:
                self.conn.execute("DROP TABLE cash_reconciliations")
            legacy_nav = self.setting("portfolio_nav_usd")
            if legacy_nav and self.portfolio_nav(1) is None:
                self.set_portfolio_nav(1, float(legacy_nav))
            if legacy_nav:
                self.conn.execute("UPDATE settings SET value='' WHERE key='portfolio_nav_usd'")
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_positions_portfolio ON positions(portfolio_id)")
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_portfolio ON trades(portfolio_id)")
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_position ON trades(position_id)")
            # Ingest and matching look these up per record; without them SQLite
            # scans broker_source_records and trade_matches on every write.
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_source_records_trade ON broker_source_records(trade_row_id)")
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_matches_buy ON trade_matches(buy_trade_id)")
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_matches_sell ON trade_matches(sell_trade_id)")
        for row in self.conn.execute("""SELECT DISTINCT portfolio_id,display_ticker,ib_symbol FROM trades
            WHERE position_id IS NULL AND display_ticker<>''""").fetchall():
            position = self.conn.execute("""SELECT id FROM positions WHERE portfolio_id=? AND display_ticker=?
                ORDER BY id LIMIT 1""", (row["portfolio_id"], row["display_ticker"])).fetchone()
            position_id = position[0] if position else self.add_position({
                "portfolio_id": row["portfolio_id"], "display_ticker": row["display_ticker"],
                "ib_symbol": row["ib_symbol"] or row["display_ticker"].split()[0]})
            with self.conn:
                self.conn.execute("""UPDATE trades SET position_id=? WHERE portfolio_id=?
                    AND display_ticker=? AND position_id IS NULL""",
                    (position_id, row["portfolio_id"], row["display_ticker"]))
        self._normalize_option_labels()

    def _normalize_option_labels(self):
        option_ids = [row[0] for row in self.conn.execute(
            "SELECT id FROM positions WHERE UPPER(security_type)='OPT'")]
        with self.conn:
            for position_id in option_ids:
                self._set_option_label(position_id)

    def _set_option_label(self, position_id):
        position = self.conn.execute("""SELECT id,portfolio_id,display_ticker,ib_symbol,
            expiry,option_right,strike,con_id FROM positions WHERE id=?""", (position_id,)).fetchone()
        if position is None:
            return
        label = option_label(position["ib_symbol"], position["expiry"],
                             position["option_right"], position["strike"])
        if label is None:
            return
        conflict = self.conn.execute("""SELECT 1 FROM positions WHERE portfolio_id=?
            AND display_ticker=? AND id<>? LIMIT 1""",
            (position["portfolio_id"], label, position_id)).fetchone()
        if conflict:
            label = f"{label} [{position['con_id'] or position_id}]"
        if label != position["display_ticker"]:
            self.conn.execute("UPDATE positions SET display_ticker=? WHERE id=?", (label, position_id))
            self.conn.execute("UPDATE trades SET display_ticker=? WHERE position_id=?",
                              (label, position_id))

    def _add_missing_columns(self, table, columns):
        existing = {row[1] for row in self.conn.execute(f"PRAGMA table_info({table})")}
        added = set()
        with self.conn:
            for name, definition in columns.items():
                if name not in existing:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
                    added.add(name)
        return added

    def rows(self, table, portfolio_id=None):
        if table not in ("positions", "trades", "trade_matches"):
            raise ValueError(table)
        if table == "trades":
            query = "SELECT * FROM trades" + (" WHERE portfolio_id=?" if portfolio_id is not None else "") + " ORDER BY id"
            raw = [dict(r) for r in self.conn.execute(query, (portfolio_id,) if portfolio_id is not None else ())]
            refunds = {r["trade_row_id"]: r["refund"] for r in self.conn.execute(
                "SELECT trade_row_id,SUM(CAST(refund_usd AS REAL)) AS refund FROM trade_fee_adjustments GROUP BY trade_row_id")}
            pending_fees = {r[0] for r in self.conn.execute(
                "SELECT trade_row_id FROM broker_executions WHERE status LIKE '%commission%'")}
            for row in raw:
                row["commission_complete"] = row["id"] not in pending_fees
                row["raw_commission_usd"] = row["commission_usd"]
                row["commission_refund_usd"] = refunds.get(row["id"], 0.0)
                row["commission_usd"] -= row["commission_refund_usd"]
            return raw
        if portfolio_id is not None:
            if table == "trade_matches":
                return [dict(r) for r in self.conn.execute("""SELECT m.* FROM trade_matches m
                    JOIN trades b ON b.id=m.buy_trade_id JOIN trades s ON s.id=m.sell_trade_id
                    WHERE b.portfolio_id=? AND s.portfolio_id=? ORDER BY m.match_id""",
                    (portfolio_id, portfolio_id))]
            return [dict(r) for r in self.conn.execute(
                f"SELECT * FROM {table} WHERE portfolio_id=? ORDER BY id", (portfolio_id,))]
        return [dict(r) for r in self.conn.execute(f"SELECT * FROM {table} ORDER BY id" if table != "trade_matches" else
                                                   "SELECT * FROM trade_matches ORDER BY match_id")]

    def add_portfolio(self, name):
        with self.conn:
            return self.conn.execute("INSERT INTO portfolios(name) VALUES (?)", (name,)).lastrowid

    def portfolio_nav(self, portfolio_id):
        row = self.conn.execute("SELECT nav_usd FROM portfolios WHERE id=?", (portfolio_id,)).fetchone()
        if row is None:
            raise ValueError("Unknown portfolio")
        return row[0]

    def set_portfolio_nav(self, portfolio_id, value):
        with self.conn:
            cur = self.conn.execute("UPDATE portfolios SET nav_usd=? WHERE id=?", (value, portfolio_id))
            if cur.rowcount != 1:
                raise ValueError("Unknown portfolio")

    def effective_nav(self, portfolio_id):
        current = self.account_value(portfolio_id, "NetLiquidation")
        if current and current["currency"] == "USD":
            return current["value"]
        return self.portfolio_nav(portfolio_id) if self.setting("provider") == "Mock" else None

    def set_account_value(self, portfolio_id, metric, value, currency, as_of):
        with self.conn:
            self.conn.execute("""INSERT INTO account_values(portfolio_id,metric,value,currency,as_of)
                VALUES (?,?,?,?,?) ON CONFLICT(portfolio_id,metric) DO UPDATE SET
                value=excluded.value,currency=excluded.currency,as_of=excluded.as_of""",
                (portfolio_id, metric, float(value), currency, as_of))
            if currency == "USD" and metric in ("NetLiquidation", "TotalCashValue"):
                day = as_of[:10]
                if metric == "NetLiquidation":
                    cash_row = self.conn.execute("""SELECT value FROM account_values
                        WHERE portfolio_id=? AND metric='TotalCashValue' AND currency='USD'
                        AND substr(as_of,1,10)=?""", (portfolio_id, day)).fetchone()
                    pnl_row = self.conn.execute("""SELECT value FROM account_values
                        WHERE portfolio_id=? AND metric='DailyPnL' AND currency='USD'
                        AND substr(as_of,1,10)=?""", (portfolio_id, day)).fetchone()
                    self.conn.execute("""INSERT INTO account_history
                        (portfolio_id,day,nav_usd,cash_usd,daily_pnl_usd,source)
                        VALUES (?,?,?,?,?,'live')
                        ON CONFLICT(portfolio_id,day) DO UPDATE SET
                        nav_usd=excluded.nav_usd,cash_usd=excluded.cash_usd
                        WHERE account_history.source='live'""",
                        (portfolio_id, day, float(value), cash_row[0] if cash_row else None,
                         pnl_row[0] if pnl_row else None))
                else:
                    self.conn.execute("""UPDATE account_history SET cash_usd=?
                        WHERE portfolio_id=? AND day=? AND source='live'""",
                        (float(value), portfolio_id, day))

    def set_daily_pnl(self, portfolio_id, value, as_of):
        """Persist the account's current-session P&L beside an observed NAV."""
        self.set_account_value(portfolio_id, "DailyPnL", value, "USD", as_of)
        with self.conn:
            self.conn.execute("""UPDATE account_history SET daily_pnl_usd=?
                WHERE portfolio_id=? AND day=? AND source='live'""",
                (float(value), portfolio_id, as_of[:10]))

    def save_account_history(self, portfolio_id, day, nav_usd, cash_usd=None,
                             source="flex", flow_covered=False):
        date.fromisoformat(day)
        if float(nav_usd) < 0:
            raise ValueError("NAV cannot be negative")
        with self.conn:
            self.conn.execute("""INSERT INTO account_history
                (portfolio_id,day,nav_usd,cash_usd,source,flow_covered) VALUES (?,?,?,?,?,?)
                ON CONFLICT(portfolio_id,day) DO UPDATE SET
                nav_usd=excluded.nav_usd,cash_usd=excluded.cash_usd,
                source=excluded.source,flow_covered=excluded.flow_covered
                WHERE excluded.source!='live' OR account_history.source='live'""",
                (portfolio_id, day, float(nav_usd), cash_usd, source, int(flow_covered)))

    def save_external_flow(self, portfolio_id, source_id, day, amount_usd, kind):
        date.fromisoformat(day)
        with self.conn:
            self.conn.execute("""INSERT INTO external_flows
                (portfolio_id,source_id,day,amount_usd,kind) VALUES (?,?,?,?,?)
                ON CONFLICT(portfolio_id,source_id) DO UPDATE SET
                day=excluded.day,amount_usd=excluded.amount_usd,kind=excluded.kind""",
                (portfolio_id, source_id, day, float(amount_usd), kind))

    def completed_flex_windows(self, account_id):
        """Saved complete periods for the selected account, including prior runs."""
        sources = {"flex": "trades", "flex_analytics": "analytics", "flex_cash": "cash"}
        periods = {label: [] for label in sources.values()}
        if account_id:
            for row in self.conn.execute("""SELECT source,from_day,to_day FROM broker_sync_windows
                    WHERE account_id=? AND status='complete'
                      AND source IN ('flex','flex_analytics','flex_cash')
                    ORDER BY source,from_day,to_day""", (account_id,)):
                periods[sources[row["source"]]].append((row["from_day"], row["to_day"]))
        return periods

    def analytics_history(self, portfolio_id):
        snapshots = [dict(row) for row in self.conn.execute(
            "SELECT * FROM account_history WHERE portfolio_id=? ORDER BY day", (portfolio_id,))]
        account_id = self.setting("ib_account_id")
        flows = [dict(row) for row in self.conn.execute(
            """SELECT * FROM external_flows AS flow WHERE portfolio_id=?
               AND NOT ((source_id LIKE 'cash:%' OR source_id LIKE 'history-csv:%')
                 AND EXISTS (SELECT 1 FROM broker_sync_windows AS window
                   WHERE window.source='flex_analytics' AND window.status='complete'
                     AND flow.day BETWEEN window.from_day AND window.to_day
                     AND (?='' OR window.account_id=?)))
               ORDER BY day""", (portfolio_id, account_id, account_id))]
        return snapshots, flows

    def monthly_history(self, portfolio_id):
        account_id = self.setting("ib_account_id")
        return [dict(row) for row in self.conn.execute(
            """SELECT month,period_from,period_to,nav_usd,return_pct
               FROM account_monthly_history WHERE portfolio_id=? AND account_id=?
               ORDER BY month""", (portfolio_id, account_id))]

    def reported_account_performance(self, portfolio_id):
        account_id = self.setting("ib_account_id")
        row = self.conn.execute(
            """SELECT period_from,period_to,cumulative_return_pct,ending_nav_usd,measure
               FROM legacy_account_performance WHERE portfolio_id=? AND account_id=?
               ORDER BY period_to DESC LIMIT 1""", (portfolio_id, account_id)).fetchone()
        return dict(row) if row else None

    def legacy_opening_quantities(self, portfolio_id):
        account_id = self.setting("ib_account_id")
        rows = self.conn.execute(
            """SELECT position_id,as_of,quantity FROM legacy_position_openings
               WHERE portfolio_id=? AND account_id=? ORDER BY position_id,as_of DESC""",
            (portfolio_id, account_id))
        result = {}
        for row in rows:
            result.setdefault(row["position_id"], dict(row))
        return result

    def legacy_statement_periods(self, portfolio_id):
        account_id = self.setting("ib_account_id")
        return [dict(row) for row in self.conn.execute(
            """SELECT period_from,period_to,event_count FROM legacy_statement_sources
               WHERE portfolio_id=? AND account_id=? ORDER BY period_from""",
            (portfolio_id, account_id))]

    def has_legacy_history(self, portfolio_id):
        account_id = self.setting("ib_account_id")
        return any(self.conn.execute(
            f"SELECT 1 FROM {table} WHERE portfolio_id=? AND account_id=? LIMIT 1",
            (portfolio_id, account_id)).fetchone() for table in (
                "account_monthly_history", "legacy_account_performance",
                "legacy_position_openings", "legacy_statement_sources",
                "legacy_statement_rows"))

    def cash_movement_rows(self, portfolio_id):
        return [dict(row) for row in self.conn.execute(
            """SELECT * FROM cash_movements WHERE portfolio_id=?
               ORDER BY reported_day DESC, source_section, source_key""", (portfolio_id,))]

    def account_value(self, portfolio_id, metric):
        row = self.conn.execute("""SELECT * FROM account_values WHERE portfolio_id=? AND metric=?""",
                                (portfolio_id, metric)).fetchone()
        return dict(row) if row else None

    def add_position(self, values=None):
        values = values or {}
        self.portfolio_nav(values.get("portfolio_id", 1))
        with self.conn:
            cur = self.conn.execute("INSERT INTO positions(portfolio_id,display_ticker,ib_symbol) VALUES (?,?,?)",
                                    (values.get("portfolio_id", 1), values.get("display_ticker", ""), values.get("ib_symbol", "")))
            row_id = cur.lastrowid
            for key, value in sorted(values.items(), key=lambda item: item[0] == "multiplier"):
                if key not in ("portfolio_id", "display_ticker", "ib_symbol"):
                    self.update("positions", row_id, key, value)
        return row_id

    def add_trade(self, values=None):
        values = dict(values or {})
        self.portfolio_nav(values.get("portfolio_id", 1))
        position_id = values.get("position_id")
        position = self.conn.execute("SELECT * FROM positions WHERE id=? AND portfolio_id=?",
                                     (position_id, values.get("portfolio_id", 1))).fetchone() if position_id else None
        if position_id and position is None:
            raise ValueError("Instrument is outside this portfolio")
        if position is None and values.get("display_ticker"):
            position = self.conn.execute("SELECT * FROM positions WHERE portfolio_id=? AND display_ticker=? ORDER BY id LIMIT 1",
                                         (values.get("portfolio_id", 1), values["display_ticker"])).fetchone()
        if position is None:
            ticker = str(values.get("display_ticker") or values.get("ib_symbol") or "").strip()
            if not ticker:
                raise ValueError("Select an instrument before entering a trade")
            position_id = self.add_position({"portfolio_id": values.get("portfolio_id", 1),
                                             "display_ticker": ticker, "ib_symbol": values.get("ib_symbol") or ticker.split()[0]})
            position = self.conn.execute("SELECT * FROM positions WHERE id=?", (position_id,)).fetchone()
        values["position_id"] = position["id"]
        for key in ("display_ticker", "ib_symbol", "security_type", "multiplier", "currency"):
            values.setdefault(key, position[key])
        if "fx_to_usd" not in values:
            currency = str(values["currency"] or "USD").upper()
            values["fx_to_usd"] = 1 if currency == "USD" else self.fx_rate(currency, values.get("trade_date", datetime.now().date().isoformat())) or 0
        trade_id = values.get("trade_id") or datetime.now().strftime("T%Y%m%d%H%M%S%f")
        with self.conn:
            cur = self.conn.execute("INSERT INTO trades(portfolio_id,position_id,trade_id,trade_date,executed_at,source) VALUES (?,?,?,?,?,?)",
                                    (values.get("portfolio_id", 1), values["position_id"], trade_id,
                                     values.get("trade_date", datetime.now().date().isoformat()),
                                     values.get("executed_at", ""),
                                     values.get("source", "manual")))
            row_id = cur.lastrowid
            for key, value in sorted(values.items(), key=lambda item: item[0] == "multiplier"):
                if key not in ("portfolio_id", "position_id", "trade_id", "trade_date", "executed_at", "source"):
                    self.update("trades", row_id, key, value)
        return row_id

    def set_fx_rate(self, currency, day, usd_per_unit, source="IBKR"):
        with self.conn:
            self.conn.execute("""INSERT INTO fx_rates(currency,day,usd_per_unit,source) VALUES (?,?,?,?)
                ON CONFLICT(currency,day) DO UPDATE SET usd_per_unit=excluded.usd_per_unit,source=excluded.source""",
                (currency.upper(), day, usd_per_unit, source))
        return self.backfill_missing_fx(currency)

    def save_fx_history(self, currency, bars, source="IBKR historical"):
        with self.conn:
            self.conn.executemany("""INSERT INTO fx_rates(currency,day,usd_per_unit,source) VALUES (?,?,?,?)
                ON CONFLICT(currency,day) DO UPDATE SET usd_per_unit=excluded.usd_per_unit,source=excluded.source""",
                [(currency.upper(), bar["day"], bar["close"], source)
                 for bar in bars if bar["close"] > 0])
        return self.backfill_missing_fx(currency)

    def backfill_missing_fx(self, currency):
        """Fill trades whose FX rate was unknown; returns how many trades changed."""
        with self.conn:
            updated = self.conn.execute("""UPDATE trades SET fx_to_usd=(
                SELECT f.usd_per_unit FROM fx_rates f WHERE f.currency=trades.currency
                AND f.day<=trades.trade_date AND julianday(trades.trade_date)-julianday(f.day)<=7
                ORDER BY f.day DESC LIMIT 1)
                WHERE currency=? AND fx_to_usd<=0 AND EXISTS (
                SELECT 1 FROM fx_rates f WHERE f.currency=trades.currency AND f.day<=trades.trade_date
                AND julianday(trades.trade_date)-julianday(f.day)<=7)""",
                (currency.upper(),)).rowcount
        return updated

    def fx_rate(self, currency, day):
        if currency.upper() == "USD":
            return 1.0
        row = self.conn.execute("""SELECT day,usd_per_unit FROM fx_rates WHERE currency=? AND day<=?
            ORDER BY day DESC LIMIT 1""", (currency.upper(), day)).fetchone()
        return row[1] if row and (date.fromisoformat(day) - date.fromisoformat(row[0])).days <= 7 else None

    def save_broker_positions(self, portfolio_id, rows, as_of):
        with self.conn:
            self.conn.execute("DELETE FROM broker_positions WHERE portfolio_id=?", (portfolio_id,))
            self.conn.executemany("""INSERT INTO broker_positions
                (portfolio_id,con_id,quantity,symbol,currency,as_of) VALUES (?,?,?,?,?,?)""",
                [(portfolio_id, int(row["con_id"]), float(row["quantity"]), row.get("symbol", ""),
                  row.get("currency", ""), as_of) for row in rows])

    def save_broker_position_delta(self, portfolio_id, row, as_of):
        with self.conn:
            if abs(float(row["quantity"])) < 1e-9:
                self.conn.execute("DELETE FROM broker_positions WHERE portfolio_id=? AND con_id=?",
                                  (portfolio_id, int(row["con_id"])))
            else:
                self.conn.execute("""INSERT INTO broker_positions
                    (portfolio_id,con_id,quantity,symbol,currency,as_of) VALUES (?,?,?,?,?,?)
                    ON CONFLICT(portfolio_id,con_id) DO UPDATE SET quantity=excluded.quantity,
                    symbol=excluded.symbol,currency=excluded.currency,as_of=excluded.as_of""",
                    (portfolio_id, int(row["con_id"]), float(row["quantity"]), row.get("symbol", ""),
                     row.get("currency", ""), as_of))

    def broker_positions(self, portfolio_id):
        return [dict(r) for r in self.conn.execute("SELECT * FROM broker_positions WHERE portfolio_id=? ORDER BY con_id",
                                                   (portfolio_id,))]

    def update(self, table, row_id, field, value):
        allowed = POSITION_FIELDS if table == "positions" else TRADE_FIELDS if table == "trades" else ()
        if field not in allowed:
            raise ValueError(field)
        with self.conn:
            if field == "security_type":
                old = self.conn.execute(f"SELECT security_type FROM {table} WHERE id=?", (row_id,)).fetchone()
                if old and old[0] != value:
                    self.conn.execute(f"UPDATE {table} SET multiplier=? WHERE id=?",
                                      (1 if value == "STK" else None, row_id))
            self.conn.execute(f"UPDATE {table} SET {field}=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", (value, row_id))
            if table == "positions" and field in ("ib_symbol", "security_type", "exchange", "primary_exchange", "currency",
                                                   "local_symbol", "expiry", "option_right", "strike", "trading_class"):
                self.conn.execute("DELETE FROM contracts WHERE position_id=?", (row_id,))
                self.conn.execute("UPDATE positions SET con_id=NULL WHERE id=?", (row_id,))
            if table == "positions" and field in ("ib_symbol", "security_type", "expiry", "option_right", "strike"):
                security_type = self.conn.execute("SELECT security_type FROM positions WHERE id=?",
                                                  (row_id,)).fetchone()
                if security_type and security_type[0] == "OPT":
                    self._set_option_label(row_id)

    def delete(self, table, row_id):
        if table not in ("positions", "trades"):
            raise ValueError(table)
        with self.conn:
            self.conn.execute(f"DELETE FROM {table} WHERE id=?", (row_id,))

    def setting(self, key):
        row = self.conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else ""

    def validate_account_change(self, account_id):
        current = self.setting("ib_account_id")
        if account_id == current:
            self.validate_account_ownership(account_id)
            return
        populated = any(self.conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
                        for table in ("positions", "trades", "broker_positions", "account_values",
                                      "account_history", "account_monthly_history", "legacy_account_performance",
                                      "legacy_statement_sources", "cash_movements", "broker_executions"))
        if populated:
            raise ValueError("This database already contains portfolio data. Use a separate database for another IBKR account.")

    def validate_account_ownership(self, account_id):
        if not account_id:
            return
        configured = self.setting("ib_account_id")
        if configured and configured != account_id:
            raise ValueError("IBKR account differs from the selected portfolio account")
        for table in ("broker_executions", "broker_source_records", "broker_sync_windows",
                      "cash_movements", "trade_fee_adjustments", "account_monthly_history",
                      "legacy_account_performance", "legacy_position_openings",
                      "legacy_statement_sources", "legacy_statement_rows"):
            if self.conn.execute(f"SELECT 1 FROM {table} WHERE account_id<>? LIMIT 1",
                                 (account_id,)).fetchone():
                raise ValueError("This database contains records from another IBKR account. Use a separate database.")

    def set_setting(self, key, value):
        with self.conn:
            self.conn.execute("INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                              (key, str(value)))

    def set_metadata(self, key, value):
        with self.conn:
            self.conn.execute("INSERT INTO app_metadata(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                              (key, str(value)))

    def metadata(self, key):
        row = self.conn.execute("SELECT value FROM app_metadata WHERE key=?", (key,)).fetchone()
        return row[0] if row else ""

    def history(self, con_id):
        return [dict(r) for r in self.conn.execute("SELECT * FROM historical_prices WHERE con_id=? ORDER BY day", (con_id,))]

    def benchmark_history(self, symbol="SPX"):
        return [dict(row) for row in self.conn.execute(
            "SELECT day, close FROM benchmark_prices WHERE symbol=? ORDER BY day", (symbol,))]

    def benchmark_latest_day(self, symbol="SPX"):
        row = self.conn.execute(
            "SELECT MAX(day) FROM benchmark_prices WHERE symbol=?", (symbol,)).fetchone()
        return row[0] if row else None

    def save_benchmark_history(self, bars, symbol="SPX"):
        valid = []
        for bar in bars:
            try:
                day = date.fromisoformat(bar["day"]).isoformat()
                close = float(bar["close"])
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
            if math.isfinite(close) and close > 0:
                valid.append((symbol, day, close))
        if valid:
            with self.conn:
                self.conn.executemany(
                    "INSERT OR REPLACE INTO benchmark_prices(symbol,day,close) VALUES (?,?,?)", valid)
        return len(valid)

    def cached_quotes(self, portfolio_id, account_id):
        if not account_id:
            return {}
        return {row["position_id"]: dict(row) for row in self.conn.execute("""
            SELECT q.* FROM market_quotes q JOIN positions p ON p.id=q.position_id
            WHERE q.portfolio_id=? AND q.account_id=? AND p.portfolio_id=?
              AND p.con_id IS q.con_id""", (portfolio_id, account_id, portfolio_id))}

    def save_quotes(self, portfolio_id, account_id, quotes):
        if not account_id or not quotes:
            return
        # A Flex sync may hold the writer lock. Quote caching is optional, so skip
        # that batch rather than blocking the UI behind the main 30-second timeout.
        try:
            with closing(sqlite3.connect(self.path, timeout=0.05)) as cache_conn:
                with cache_conn:
                    cache_conn.executemany("""INSERT INTO market_quotes
                        (portfolio_id,account_id,position_id,con_id,price,source,as_of)
                        VALUES (?,?,?,?,?,?,?) ON CONFLICT(portfolio_id,account_id,position_id)
                        DO UPDATE SET con_id=excluded.con_id,price=excluded.price,
                                      source=excluded.source,as_of=excluded.as_of""",
                        [(portfolio_id, account_id, *quote) for quote in quotes])
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower():
                raise

    def save_history(self, con_id, bars):
        with self.conn:
            self.conn.executemany("INSERT OR REPLACE INTO historical_prices(con_id,day,close,volume) VALUES (?,?,?,?)",
                                  [(con_id, b["day"], b["close"], b.get("volume")) for b in bars])
        self.set_metadata(f"history_refresh_{con_id}", datetime.now().date().isoformat())

    def cache_contract(self, position_id, con_id, time_zone="", multiplier=None, details=None):
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO contracts(position_id,con_id,time_zone) VALUES (?,?,?)",
                              (position_id, con_id, time_zone))
            self.conn.execute("UPDATE positions SET con_id=? WHERE id=?", (con_id, position_id))
            if multiplier is not None and float(multiplier) > 0:
                security_type = (details or {}).get("security_type") or self.conn.execute(
                    "SELECT security_type FROM positions WHERE id=?", (position_id,)).fetchone()[0]
                effective_multiplier = float(multiplier)
                if security_type == "BOND" and effective_multiplier >= 1:
                    effective_multiplier /= 100
                self.conn.execute("UPDATE positions SET multiplier=? WHERE id=?", (effective_multiplier, position_id))
                self.conn.execute("UPDATE trades SET multiplier=? WHERE position_id=? AND multiplier IS NULL",
                                  (effective_multiplier, position_id))
            if details:
                for field in ("ib_symbol", "security_type", "currency", "exchange", "primary_exchange",
                              "local_symbol", "expiry", "option_right", "strike", "trading_class"):
                    source = "symbol" if field == "ib_symbol" else field
                    if details.get(source) not in (None, ""):
                        self.conn.execute(f"UPDATE positions SET {field}=? WHERE id=?",
                                          (details[source], position_id))
                if details.get("security_type") == "OPT":
                    self._set_option_label(position_id)

    def backup(self, directory=None):
        target_dir = Path(directory or self.setting("backup_directory") or self.path.parent / "backups")
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"portfolio-{datetime.now():%Y%m%d-%H%M%S-%f}.sqlite3"
        destination = sqlite3.connect(target)
        try:
            self.conn.backup(destination)
        finally:
            destination.close()
        return target

    def configure_data_directory(self, directory):
        """Copy a quiescent database and select the verified copy for next launch."""
        root = Path(directory) if directory else default_data_dir()
        target_dir = new_portfolio_dir(root)
        target = target_dir / "portfolio.sqlite3"
        temporary = target_dir / f".portfolio-{uuid4().hex}.sqlite3"
        created = False
        try:
            with closing(sqlite3.connect(temporary)) as destination:
                self.conn.backup(destination)
                if destination.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise RuntimeError("Copied portfolio database failed integrity check")
            os.replace(temporary, target)
            created = True
        except Exception:
            temporary.unlink(missing_ok=True)
            target_dir.rmdir()
            raise
        config_path = local_config_path()
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_temp = config_path.with_name(f".{config_path.name}-{uuid4().hex}.tmp")
        try:
            config_temp.write_text(json.dumps({"data_directory": str(target_dir)}), encoding="utf-8")
            os.replace(config_temp, config_path)
        except Exception:
            config_temp.unlink(missing_ok=True)
            if created:
                target.unlink(missing_ok=True)
                target_dir.rmdir()
            raise
