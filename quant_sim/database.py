import sqlite3
import json
from datetime import datetime
import os
import threading

class Database:
    def __init__(self, db_path="quant_sim.db"):
        self.db_path = db_path
        self._local = threading.local()
        self.init_db()

    def get_connection(self):
        """获取线程本地的数据库连接"""
        if not hasattr(self._local, "connection"):
            self._local.connection = sqlite3.connect(self.db_path, check_same_thread=False)
            self._local.connection.row_factory = sqlite3.Row
        return self._local.connection

    def init_db(self):
        conn = self.get_connection()
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
        
        # 决策快照表
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
        columns = {row['name'] for row in cursor.fetchall()}
        if column_name not in columns:
            cursor.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}")

    def get_account(self, initial_capital=100000.0):
        conn = self.get_connection()
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
        return dict(row)

    def reset_simulation(self, initial_capital=100000.0):
        conn = self.get_connection()
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
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT initial_capital FROM account WHERE id = 1")
        row = cursor.fetchone()
        initial = row['initial_capital'] if row else 100000.0
        total_pnl = total_assets - initial
        cursor.execute(
            "UPDATE account SET balance = ?, total_assets = ?, total_pnl = ?, updated_at = ? WHERE id = 1",
            (balance, total_assets, total_pnl, datetime.now().isoformat())
        )
        conn.commit()

    def execute_trade(self, symbol, name, action, price, quantity, fee, reason, timestamp=None):
        conn = self.get_connection()
        cursor = conn.cursor()
        if timestamp is None:
            timestamp = datetime.now().isoformat()
        cursor.execute(
            "INSERT INTO trades (symbol, name, action, price, quantity, fee, timestamp, reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (symbol, name, action, price, quantity, fee, timestamp, reason)
        )
        conn.commit()

    def get_positions(self):
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM positions")
        rows = cursor.fetchall()
        return [dict(r) for r in rows]

    def update_position(self, symbol, name, quantity, avg_price, current_price, target_price, stop_loss_price, bought_at=None):
        conn = self.get_connection()
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
            (symbol, name, quantity, avg_price, current_price, bought_at,
             target_price, stop_loss_price, 0, current_price, 0)
        )
        conn.commit()

    def remove_position(self, symbol):
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM positions WHERE symbol = ?", (symbol,))
        conn.commit()

    def update_position_price(self, symbol, current_price):
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("UPDATE positions SET current_price = ? WHERE symbol = ?", (current_price, symbol))
        conn.commit()

    def update_position_state(self, symbol, **fields):
        allowed_fields = {
            "quantity", "avg_price", "current_price", "target_price",
            "stop_loss_price", "trailing_active", "high_water_price",
            "partial_exit_done", "bought_at",
        }
        updates = {key: value for key, value in fields.items() if key in allowed_fields}
        if not updates:
            return

        assignments = ", ".join(f"{key} = ?" for key in updates)
        values = list(updates.values()) + [symbol]
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute(f"UPDATE positions SET {assignments} WHERE symbol = ?", values)
        conn.commit()

    def log_decision(self, prompt, kb_quote, logic, raw_response):
        conn = self.get_connection()
        cursor = conn.cursor()
        timestamp = datetime.now().isoformat()
        cursor.execute(
            "INSERT INTO decision_logs (timestamp, prompt, kb_quote, logic, raw_response) VALUES (?, ?, ?, ?, ?)",
            (timestamp, prompt, kb_quote, logic, raw_response)
        )
        conn.commit()

    def log_mda_snapshot(self, action, symbol, name, total_score, win_rate_confidence, dimension_scores,
                        risk_text, lock_status, thinking_trace, logic, reason):
        dimension_scores = dimension_scores or {}
        thinking_trace_text = json.dumps(thinking_trace or {}, ensure_ascii=False)
        logic_text = json.dumps(logic or {}, ensure_ascii=False)
        conn = self.get_connection()
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
                datetime.now().isoformat(), action, symbol, name, total_score, win_rate_confidence,
                float(dimension_scores.get("data_arch", 0.0) or 0.0),
                float(dimension_scores.get("notebooklm", 0.0) or 0.0),
                float(dimension_scores.get("game_psych", 0.0) or 0.0),
                float(dimension_scores.get("trend", 0.0) or 0.0),
                risk_text, lock_status, thinking_trace_text, logic_text, reason,
            )
        )
        conn.commit()

    def get_latest_mda_snapshot(self):
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT * FROM mda_snapshots
            ORDER BY timestamp DESC
            LIMIT 1
            """
        )
        row = cursor.fetchone()
        if not row:
            return None
        
        data = dict(row)
        return {
            "timestamp": data["timestamp"],
            "action": data["action"],
            "symbol": data["symbol"],
            "name": data["name"],
            "total_score": data["total_score"],
            "win_rate_confidence": data["win_rate_confidence"],
            "dimension_scores": {
                "data_arch": data["data_arch_score"],
                "notebooklm": data["notebook_score"],
                "game_psych": data["game_score"],
                "trend": data["trend_score"],
            },
            "risk_text": data["risk_text"],
            "lock_status": data["lock_status"],
            "thinking_trace": json.loads(data["thinking_trace"]) if data["thinking_trace"] else {},
            "logic": json.loads(data["logic"]) if data["logic"] else {},
            "reason": data["reason"],
        }
