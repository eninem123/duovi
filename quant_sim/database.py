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
                    stop_loss_price REAL
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
            conn.commit()

    def get_account(self, initial_capital=1000000.0):
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

    def execute_trade(self, symbol, name, action, price, quantity, fee, reason):
        with self.get_connection() as conn:
            cursor = conn.cursor()
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
                    "stop_loss_price": r[7]
                } for r in rows
            ]

    def update_position(self, symbol, name, quantity, avg_price, current_price, target_price, stop_loss_price):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            bought_at = datetime.now().isoformat()
            cursor.execute(
                "INSERT OR REPLACE INTO positions (symbol, name, quantity, avg_price, current_price, bought_at, target_price, stop_loss_price) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (symbol, name, quantity, avg_price, current_price, bought_at, target_price, stop_loss_price)
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

    def log_decision(self, prompt, kb_quote, logic, raw_response):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            timestamp = datetime.now().isoformat()
            cursor.execute(
                "INSERT INTO decision_logs (timestamp, prompt, kb_quote, logic, raw_response) VALUES (?, ?, ?, ?, ?)",
                (timestamp, prompt, kb_quote, logic, raw_response)
            )
            conn.commit()
