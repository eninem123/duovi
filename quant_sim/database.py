import sqlite3
import json
from datetime import datetime
import os

class Database:
    def __init__(self, db_path="quant_sim.db"):
        self.db_path = db_path
        self.init_db()

    def get_connection(self):
        return sqlite3.connect(self.db_path)

    def init_db(self):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            # 账户表
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS account (
                    id INTEGER PRIMARY KEY,
                    initial_capital REAL,
                    balance REAL,
                    total_assets REAL,
                    total_pnl REAL,
                    updated_at TEXT
                )
            ''')
            
            # 持仓表
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS positions (
                    symbol TEXT PRIMARY KEY,
                    name TEXT,
                    quantity INTEGER,
                    avg_price REAL,
                    current_price REAL,
                    bought_at TEXT,
                    target_price REAL,
                    stop_loss_price REAL,
                    trailing_active INTEGER DEFAULT 0,
                    high_water_price REAL DEFAULT 0,
                    partial_exit_done INTEGER DEFAULT 0
                )
            ''')
            
            # 交易流水表
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT,
                    name TEXT,
                    action TEXT,
                    price REAL,
                    quantity INTEGER,
                    fee REAL,
                    timestamp TEXT,
                    reason TEXT
                )
            ''')
            
            # 决策日志表
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS decision_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT,
                    prompt TEXT,
                    kb_quote TEXT,
                    logic TEXT,
                    raw_response TEXT
                )
            ''')
            
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS mda_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT,
                    action TEXT,
                    symbol TEXT,
                    name TEXT,
                    total_score REAL,
                    win_rate_confidence REAL,
                    data_arch_score REAL,
                    notebook_score REAL,
                    game_score REAL,
                    trend_score REAL,
                    risk_text TEXT,
                    lock_status TEXT,
                    thinking_trace TEXT,
                    logic TEXT,
                    reason TEXT
                )
            ''')

            self._ensure_column(cursor, "positions", "trailing_active", "INTEGER DEFAULT 0")
            self._ensure_column(cursor, "positions", "high_water_price", "REAL DEFAULT 0")
            self._ensure_column(cursor, "positions", "partial_exit_done", "INTEGER DEFAULT 0")
            conn.commit()

    def _ensure_column(self, cursor, table_name, column_name, definition):
        cursor.execute(f"PRAGMA table_info({table_name})")
        columns = {row[1] for row in cursor.fetchall()}
        if column_name not in columns:
            cursor.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}")

    def get_account(self, initial_capital=100000.0):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM account WHERE id = 1")
            row = cursor.fetchone()
            if not row:
                cursor.execute(
                    "INSERT INTO account (id, initial_capital, balance, total_assets, total_pnl, updated_at) VALUES (1, ?, ?, ?, 0.0, ?)",
                    (initial_capital, initial_capital, initial_capital, datetime.now().isoformat())
                )
                conn.commit()
                return {"initial_capital": initial_capital, "balance": initial_capital, "total_assets": initial_capital, "total_pnl": 0.0}
            return {
                "initial_capital": row[1],
                "balance": row[2],
                "total_assets": row[3],
                "total_pnl": row[4]
            }

    def reset_simulation(self, initial_capital=100000.0):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM positions")
            cursor.execute("DELETE FROM trades")
            cursor.execute("DELETE FROM decision_logs")
            cursor.execute("DELETE FROM mda_snapshots")
            cursor.execute("DELETE FROM sqlite_sequence WHERE name IN ('trades', 'decision_logs', 'mda_snapshots')")
            cursor.execute("DELETE FROM account WHERE id = 1")
            cursor.execute(
                """
                INSERT INTO account (id, initial_capital, balance, total_assets, total_pnl, updated_at)
                VALUES (1, ?, ?, ?, 0.0, ?)
                """,
                (initial_capital, initial_capital, initial_capital, datetime.now().isoformat()),
            )
            conn.commit()

    def update_account(self, balance, total_assets):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT initial_capital FROM account WHERE id = 1")
            initial = cursor.fetchone()[0]
            total_pnl = total_assets - initial
            cursor.execute(
                "UPDATE account SET balance = ?, total_assets = ?, total_pnl = ?, updated_at = ? WHERE id = 1",
                (balance, total_assets, total_pnl, datetime.now().isoformat())
            )
            conn.commit()

    def execute_trade(self, symbol, name, action, price, quantity, fee, reason, timestamp=None):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            if timestamp is None:
                timestamp = datetime.now().isoformat()
            cursor.execute(
                "INSERT INTO trades (symbol, name, action, price, quantity, fee, timestamp, reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (symbol, name, action, price, quantity, fee, timestamp, reason)
            )
            conn.commit()

    def get_positions(self):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM positions")
            rows = cursor.fetchall()
            return [
                {
                    "symbol": r[0],
                    "name": r[1],
                    "quantity": r[2],
                    "avg_price": r[3],
                    "current_price": r[4],
                    "bought_at": r[5],
                    "target_price": r[6],
                    "stop_loss_price": r[7],
                    "trailing_active": bool(r[8]) if len(r) > 8 else False,
                    "high_water_price": r[9] if len(r) > 9 else r[4],
                    "partial_exit_done": bool(r[10]) if len(r) > 10 else False,
                } for r in rows
            ]

    def update_position(self, symbol, name, quantity, avg_price, current_price, target_price, stop_loss_price, bought_at=None):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            if bought_at is None:
                bought_at = datetime.now().isoformat()
            cursor.execute(
                """
                INSERT OR REPLACE INTO positions (
                    symbol, name, quantity, avg_price, current_price, bought_at,
                    target_price, stop_loss_price, trailing_active, high_water_price, partial_exit_done
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    symbol,
                    name,
                    quantity,
                    avg_price,
                    current_price,
                    bought_at,
                    target_price,
                    stop_loss_price,
                    0,
                    current_price,
                    0,
                )
            )
            conn.commit()

    def remove_position(self, symbol):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM positions WHERE symbol = ?", (symbol,))
            conn.commit()

    def update_position_price(self, symbol, current_price):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE positions SET current_price = ? WHERE symbol = ?", (current_price, symbol))
            conn.commit()

    def update_position_state(self, symbol, **fields):
        allowed_fields = {
            "quantity",
            "avg_price",
            "current_price",
            "target_price",
            "stop_loss_price",
            "trailing_active",
            "high_water_price",
            "partial_exit_done",
            "bought_at",
        }
        updates = {key: value for key, value in fields.items() if key in allowed_fields}
        if not updates:
            return

        assignments = ", ".join(f"{key} = ?" for key in updates)
        values = list(updates.values()) + [symbol]
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(f"UPDATE positions SET {assignments} WHERE symbol = ?", values)
            conn.commit()

    def log_decision(self, prompt, kb_quote, logic, raw_response):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            timestamp = datetime.now().isoformat()
            cursor.execute(
                "INSERT INTO decision_logs (timestamp, prompt, kb_quote, logic, raw_response) VALUES (?, ?, ?, ?, ?)",
                (timestamp, prompt, kb_quote, logic, raw_response)
            )
            conn.commit()

    def log_mda_snapshot(
        self,
        action,
        symbol,
        name,
        total_score,
        win_rate_confidence,
        dimension_scores,
        risk_text,
        lock_status,
        thinking_trace,
        logic,
        reason,
    ):
        dimension_scores = dimension_scores or {}
        thinking_trace_text = json.dumps(thinking_trace or {}, ensure_ascii=False)
        logic_text = json.dumps(logic or {}, ensure_ascii=False)
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO mda_snapshots (
                    timestamp, action, symbol, name, total_score, win_rate_confidence,
                    data_arch_score, notebook_score, game_score, trend_score,
                    risk_text, lock_status, thinking_trace, logic, reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    datetime.now().isoformat(),
                    action,
                    symbol,
                    name,
                    total_score,
                    win_rate_confidence,
                    float(dimension_scores.get("data_arch", 0.0) or 0.0),
                    float(dimension_scores.get("notebooklm", 0.0) or 0.0),
                    float(dimension_scores.get("game_psych", 0.0) or 0.0),
                    float(dimension_scores.get("trend", 0.0) or 0.0),
                    risk_text,
                    lock_status,
                    thinking_trace_text,
                    logic_text,
                    reason,
                )
            )
            conn.commit()

    def get_latest_mda_snapshot(self):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT timestamp, action, symbol, name, total_score, win_rate_confidence,
                       data_arch_score, notebook_score, game_score, trend_score,
                       risk_text, lock_status, thinking_trace, logic, reason
                FROM mda_snapshots
                ORDER BY timestamp DESC
                LIMIT 1
                """
            )
            row = cursor.fetchone()
            if not row:
                return None
            return {
                "timestamp": row[0],
                "action": row[1],
                "symbol": row[2],
                "name": row[3],
                "total_score": row[4],
                "win_rate_confidence": row[5],
                "dimension_scores": {
                    "data_arch": row[6],
                    "notebooklm": row[7],
                    "game_psych": row[8],
                    "trend": row[9],
                },
                "risk_text": row[10],
                "lock_status": row[11],
                "thinking_trace": json.loads(row[12]) if row[12] else {},
                "logic": json.loads(row[13]) if row[13] else {},
                "reason": row[14],
            }
