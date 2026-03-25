import yaml
from database import Database
from datetime import datetime, timedelta
import logging
import math
import utils

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
        self.account = self.db.get_account(self.trading_config["initial_capital"])

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

    def get_lock_status(self, position, now=None):
        if now is None:
            now = datetime.now()
        bought_at = datetime.fromisoformat(position["bought_at"])
        lock_minutes = int(self.trading_config.get("sell_lock_minutes", 30))
        unlock_at = bought_at + timedelta(minutes=lock_minutes)
        remaining_seconds = max(0.0, (unlock_at - now).total_seconds())
        is_locked = remaining_seconds > 0
        remaining_minutes = int(math.ceil(remaining_seconds / 60.0)) if is_locked else 0
        return {
            "is_locked": is_locked,
            "remaining_minutes": remaining_minutes,
            "unlock_at": unlock_at,
        }

    def can_sell(self, position, now=None):
        """
        检查是否满足卖出条件：
        1. 遵守 A 股 T+1 规则（当天买入，次日及以后方可卖出）
        2. 遵守买入后 30 分钟锁仓协议
        """
        if now is None:
            now = datetime.now()
        
        # 基础 T+1 检查
        bought_at = datetime.fromisoformat(position["bought_at"])
        if now.date() <= bought_at.date():
            return False
            
        # 锁仓时间检查
        lock_status = self.get_lock_status(position, now=now)
        return not lock_status["is_locked"]

    def refresh_position_risk(self, position):
        current_price = float(position["current_price"])
        avg_price = float(position["avg_price"])
        high_water_price = float(position.get("high_water_price") or avg_price)
        trailing_active = bool(position.get("trailing_active"))

        updates = {}
        if current_price > high_water_price:
            updates["high_water_price"] = current_price

        trail_arm_pct = float(self.trading_config.get("trail_arm_pct", 0.10))
        trail_floor_pct = float(self.trading_config.get("trail_floor_pct", 0.05))
        if current_price >= avg_price * (1 + trail_arm_pct):
            target_stop = avg_price * (1 + trail_floor_pct)
            current_stop = float(position.get("stop_loss_price") or 0.0)
            if target_stop > current_stop:
                updates["stop_loss_price"] = target_stop
            if not trailing_active:
                updates["trailing_active"] = 1

        if updates:
            self.db.update_position_state(position["symbol"], **updates)
            position.update(updates)

        return position

    def check_exit_conditions(self, position, now=None):
        """
        检查是否触发卖出条件：
        1. 达到 +15% 后触发强制减仓
        2. 跌破均价 -5% 或移动止盈线
        3. 持仓时间超过最大天数
        """
        if now is None:
            now = datetime.now()
        position = self.refresh_position_risk(position)

        current_price = position["current_price"]
        avg_price = position["avg_price"]
        bought_at = datetime.fromisoformat(position["bought_at"])
        max_days = self.trading_config.get("max_holding_days", 15)

        if not self.can_sell(position, now=now):
            return False, None

        partial_take_at_return = float(self.trading_config.get("partial_take_at_return", 0.15))
        stop_loss_pct = abs(float(self.trading_config.get("stop_loss", -0.05)))
        hard_stop_price = avg_price * (1 - stop_loss_pct)
        trailing_stop_price = float(position.get("stop_loss_price") or hard_stop_price)

        if (not position.get("partial_exit_done")) and current_price >= avg_price * (1 + partial_take_at_return):
            return True, "Partial Take Profit"
        if current_price <= hard_stop_price:
            return True, "Stop Loss"
        if bool(position.get("trailing_active")) and current_price <= trailing_stop_price:
            return True, "Trailing Stop"
        if now - bought_at >= timedelta(days=max_days):
            return True, "Time Stop (15 days)"

        return False, None

    def buy(self, symbol, name, price, position_pct, target_price, stop_loss_price, reason, trade_time=None):
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
        trade_timestamp = trade_time.isoformat() if trade_time else None
        self.db.execute_trade(symbol, name, "BUY", price, quantity, commission, reason, timestamp=trade_timestamp)
        self.db.update_position(
            symbol, name, quantity, price, price, target_price, stop_loss_price, bought_at=trade_timestamp
        )
        self.sync_total_assets()
        
        logging.info(f"✅ 买入 {name}({symbol}): {quantity}股 @ {price:.2f}, 耗资 {total_cost:.2f}")
        return True

    def sell(self, symbol, current_price, reason, trade_time=None):
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
        trade_timestamp = trade_time.isoformat() if trade_time else None
        self.db.execute_trade(
            symbol, position["name"], "SELL", current_price, quantity, total_fee, reason, timestamp=trade_timestamp
        )
        self.db.remove_position(symbol)
        self.sync_total_assets()
        
        pnl = revenue - (position["avg_price"] * quantity)
        pnl_pct = pnl / (position["avg_price"] * quantity) * 100
        logging.info(f"💰 卖出 {position['name']}({symbol}): {quantity}股 @ {current_price:.2f}, 盈亏 {pnl:.2f} ({pnl_pct:.2f}%), 理由: {reason}")
        return True

    def sell_partial(self, symbol, current_price, fraction, reason, trade_time=None):
        positions = {p["symbol"]: p for p in self.db.get_positions()}
        if symbol not in positions:
            return False

        position = positions[symbol]
        quantity = position["quantity"]
        sell_quantity = int((quantity * fraction) // 100 * 100)
        if sell_quantity <= 0:
            logging.warning(f"[{symbol}] 当前持仓不足以按 100 股整数倍执行部分卖出。")
            return False
        if sell_quantity >= quantity:
            return self.sell(symbol, current_price, reason, trade_time=trade_time)

        commission = max(current_price * sell_quantity * self.trading_config["commission_rate"], 5.0)
        stamp_duty = current_price * sell_quantity * self.trading_config["stamp_duty"]
        total_fee = commission + stamp_duty
        revenue = current_price * sell_quantity - total_fee

        self.account["balance"] += revenue
        trade_timestamp = trade_time.isoformat() if trade_time else None
        self.db.execute_trade(
            symbol,
            position["name"],
            "SELL",
            current_price,
            sell_quantity,
            total_fee,
            reason,
            timestamp=trade_timestamp,
        )
        self.db.update_position_state(
            symbol,
            quantity=quantity - sell_quantity,
            current_price=current_price,
            partial_exit_done=1,
            high_water_price=max(float(position.get("high_water_price") or current_price), current_price),
        )
        self.sync_total_assets()

        pnl = (current_price - position["avg_price"]) * sell_quantity - total_fee
        pnl_pct = pnl / (position["avg_price"] * sell_quantity) * 100 if sell_quantity else 0.0
        logging.info(
            f"💡 减仓 {position['name']}({symbol}): {sell_quantity}股 @ {current_price:.2f}, "
            f"盈亏 {pnl:.2f} ({pnl_pct:.2f}%), 理由: {reason}"
        )
        return True

    def process_exits(self, now=None):
        """扫描并处理所有退出条件"""
        positions = self.db.get_positions()
        actions = []
        for p in positions:
            should_sell, reason = self.check_exit_conditions(p, now=now)
            if should_sell:
                if reason == "Partial Take Profit":
                    partial_take_ratio = float(self.trading_config.get("partial_take_ratio", 0.5))
                    success = self.sell_partial(p["symbol"], p["current_price"], partial_take_ratio, reason, trade_time=now)
                    if success:
                        actions.append({"action": "强制减仓", "symbol": p["symbol"], "name": p["name"], "reason": reason})
                else:
                    success = self.sell(p["symbol"], p["current_price"], reason, trade_time=now)
                    if success:
                        actions.append({"action": "卖出", "symbol": p["symbol"], "name": p["name"], "reason": reason})
        return actions

    def print_status(self):
        acc = self.db.get_account()
        logging.info(f"=== 账户状态 ===")
        logging.info(f"总资产: {acc['total_assets']:.2f}")
        logging.info(f"可用资金: {acc['balance']:.2f}")
        logging.info(f"总盈亏: {acc['total_pnl']:.2f} ({(acc['total_assets']/acc['initial_capital'] - 1)*100:.2f}%)")
        logging.info(f"================")
