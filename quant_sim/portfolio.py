import yaml
from database import Database
from datetime import datetime, timedelta
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

class PortfolioManager:
    def __init__(self, config_path="config.yaml", db_path="quant_sim.db"):
        with open(config_path, "r", encoding="utf-8") as f:
            self.config = yaml.safe_load(f)
        
        self.db = Database(db_path)
        self.trading_config = self.config["trading"]
        
        # Initialize account
        self.account = self.db.get_account(self.trading_config["initial_capital"])
        self.sync_total_assets()

    def sync_total_assets(self):
        """同步计算总资产（现金 + 股票市值）"""
        positions = self.db.get_positions()
        stock_value = sum(p["quantity"] * p["current_price"] for p in positions)
        total_assets = self.account["balance"] + stock_value
        self.db.update_account(self.account["balance"], total_assets)
        self.account = self.db.get_account()

    def update_market_prices(self, market_data_dict):
        """
        更新持仓的最新价格
        market_data_dict: dict, {symbol: current_price}
        """
        positions = self.db.get_positions()
        for p in positions:
            sym = p["symbol"]
            if sym in market_data_dict:
                self.db.update_position_price(sym, market_data_dict[sym])
        self.sync_total_assets()

    def can_sell(self, position):
        """检查是否满足最少锁仓时间（1小时）"""
        bought_at = datetime.fromisoformat(position["bought_at"])
        lock_minutes = self.trading_config.get("lock_period_minutes", 60)
        return datetime.now() - bought_at >= timedelta(minutes=lock_minutes)

    def check_exit_conditions(self, position):
        """
        检查是否触发卖出条件：
        1. 达到止盈目标价
        2. 跌破止损价
        3. 持仓时间超过最大天数(15天)
        """
        if not self.can_sell(position):
            return False, None

        current_price = position["current_price"]
        bought_at = datetime.fromisoformat(position["bought_at"])
        max_days = self.trading_config.get("max_holding_days", 15)
        
        if current_price >= position["target_price"]:
            return True, "Take Profit"
        elif current_price <= position["stop_loss_price"]:
            return True, "Stop Loss"
        elif datetime.now() - bought_at >= timedelta(days=max_days):
            return True, "Time Stop (15 days)"
            
        return False, None

    def buy(self, symbol, name, price, position_pct, target_price, stop_loss_price, reason):
        """执行买入操作"""
        # 检查是否已持仓
        positions = {p["symbol"]: p for p in self.db.get_positions()}
        if symbol in positions:
            logging.warning(f"[{symbol}] 已在持仓中，跳过买入。")
            return False

        # 计算可用资金与买入数量
        available_cash = self.account["balance"]
        total_assets = self.account["total_assets"]
        
        # 按总资产的比例计算打算买入的金额
        intended_amount = total_assets * position_pct
        if intended_amount > available_cash:
            intended_amount = available_cash
            
        # A股买入必须是100股的整数倍
        quantity = int(intended_amount / price // 100 * 100)
        if quantity == 0:
            logging.warning(f"[{symbol}] 可用资金不足以买入100股。")
            return False

        # 计算费用（买入只有佣金，无印花税）
        commission = price * quantity * self.trading_config["commission_rate"]
        # 佣金最低5元
        commission = max(commission, 5.0)
        total_cost = price * quantity + commission

        if total_cost > available_cash:
            # 重新调整数量
            quantity -= 100
            if quantity <= 0:
                return False
            commission = max(price * quantity * self.trading_config["commission_rate"], 5.0)
            total_cost = price * quantity + commission

        # 执行数据库更新
        self.account["balance"] -= total_cost
        self.db.execute_trade(symbol, name, "BUY", price, quantity, commission, reason)
        self.db.update_position(symbol, name, quantity, price, price, target_price, stop_loss_price)
        self.sync_total_assets()
        
        logging.info(f"✅ 买入 {name}({symbol}): {quantity}股 @ {price:.2f}, 耗资 {total_cost:.2f}")
        return True

    def sell(self, symbol, current_price, reason):
        """执行卖出操作"""
        positions = {p["symbol"]: p for p in self.db.get_positions()}
        if symbol not in positions:
            return False
            
        position = positions[symbol]
        quantity = position["quantity"]
        
        # 计算费用（卖出含佣金和印花税）
        commission = max(current_price * quantity * self.trading_config["commission_rate"], 5.0)
        stamp_duty = current_price * quantity * self.trading_config["stamp_duty"]
        total_fee = commission + stamp_duty
        
        revenue = current_price * quantity - total_fee
        
        # 更新账户
        self.account["balance"] += revenue
        self.db.execute_trade(symbol, position["name"], "SELL", current_price, quantity, total_fee, reason)
        self.db.remove_position(symbol)
        self.sync_total_assets()
        
        pnl = revenue - (position["avg_price"] * quantity)
        pnl_pct = pnl / (position["avg_price"] * quantity) * 100
        logging.info(f"💰 卖出 {position['name']}({symbol}): {quantity}股 @ {current_price:.2f}, 盈亏 {pnl:.2f} ({pnl_pct:.2f}%), 理由: {reason}")
        return True

    def process_exits(self):
        """扫描并处理所有退出条件"""
        positions = self.db.get_positions()
        for p in positions:
            should_sell, reason = self.check_exit_conditions(p)
            if should_sell:
                self.sell(p["symbol"], p["current_price"], reason)

    def print_status(self):
        acc = self.db.get_account()
        logging.info(f"=== 账户状态 ===")
        logging.info(f"总资产: {acc['total_assets']:.2f}")
        logging.info(f"可用资金: {acc['balance']:.2f}")
        logging.info(f"总盈亏: {acc['total_pnl']:.2f} ({(acc['total_assets']/acc['initial_capital'] - 1)*100:.2f}%)")
        logging.info(f"================")
