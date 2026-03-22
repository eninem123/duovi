import unittest
import os
from datetime import datetime, timedelta
from portfolio import PortfolioManager

class TestTradingSystem(unittest.TestCase):
    def setUp(self):
        # 使用测试数据库
        self.db_path = f"test_quant_sim_{self._testMethodName}.db"
        try:
            if os.path.exists(self.db_path):
                os.remove(self.db_path)
        except PermissionError:
            pass
            
        self.pm = PortfolioManager(config_path="config.yaml", db_path=self.db_path)

    def tearDown(self):
        try:
            if os.path.exists(self.db_path):
                os.remove(self.db_path)
        except PermissionError:
            pass

    def _set_bought_at(self, symbol, when):
        with self.pm.db.get_connection() as conn:
            conn.execute("UPDATE positions SET bought_at = ? WHERE symbol = ?", (when.isoformat(), symbol))
            conn.commit()

    def test_buy_calculation(self):
        """测试买入逻辑及手续费计算精度"""
        initial_balance = self.pm.account["balance"]
        
        # 尝试买入：价格 10.0，仓位 10% (10,000元)
        success = self.pm.buy(
            symbol="sh600598",
            name="北大荒",
            price=10.0,
            position_pct=0.1,
            target_price=11.5,
            stop_loss_price=9.5,
            reason="Test"
        )
        
        self.assertTrue(success)
        
        # 验证数量 (10000 / 10 = 1000股)
        positions = self.pm.db.get_positions()
        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0]["quantity"], 1000)
        
        # 验证手续费和余额 (1000 * 10 = 10000, 佣金 = max(10000 * 0.0003, 5.0) = 5.0)
        expected_cost = 10000.0 + 5.0
        expected_balance = initial_balance - expected_cost
        
        self.assertAlmostEqual(self.pm.account["balance"], expected_balance, places=4)

    def test_lock_period(self):
        """测试 30 分钟锁仓 + T+1 双重限制"""
        self.pm.buy("sh600598", "北大荒", 10.0, 0.1, 11.5, 9.5, "Test")
        positions = self.pm.db.get_positions()
        
        # 刚刚买入，不应该能卖出
        can_sell = self.pm.can_sell(positions[0])
        self.assertFalse(can_sell)

        # 已经过 31 分钟，但仍是当日买入，T+1 未满足，仍不可卖
        self._set_bought_at("sh600598", datetime.now() - timedelta(minutes=31))
        positions = self.pm.db.get_positions()
        can_sell = self.pm.can_sell(positions[0])
        self.assertFalse(can_sell)

        # 满足 T+1 且超过 30 分钟锁仓，允许卖出
        self._set_bought_at("sh600598", datetime.now() - timedelta(days=1, minutes=31))
        positions = self.pm.db.get_positions()
        can_sell = self.pm.can_sell(positions[0])
        self.assertTrue(can_sell)

    def test_stop_loss(self):
        """测试止损触发逻辑"""
        self.pm.buy("sh600598", "北大荒", 10.0, 0.1, 11.5, 9.5, "Test")
        
        # 修改为已过锁定期 (模拟 T+1)
        self._set_bought_at("sh600598", datetime.now() - timedelta(days=1, minutes=31))
            
        # 价格跌至 9.4 (低于均价 -5%)
        self.pm.update_market_prices({"sh600598": 9.4})
        
        positions = self.pm.db.get_positions()
        should_sell, reason = self.pm.check_exit_conditions(positions[0])
        self.assertTrue(should_sell)
        self.assertEqual(reason, "Stop Loss")

    def test_trailing_stop_activation(self):
        """测试 +10% 后启用移动止盈，回落触发卖出"""
        self.pm.buy("sh600598", "北大荒", 10.0, 0.1, 11.5, 9.5, "Test")
        self._set_bought_at("sh600598", datetime.now() - timedelta(days=1, minutes=31))

        self.pm.update_market_prices({"sh600598": 11.2})
        positions = self.pm.db.get_positions()
        self.pm.refresh_position_risk(positions[0])
        positions = self.pm.db.get_positions()
        self.assertTrue(positions[0]["trailing_active"])
        self.assertAlmostEqual(positions[0]["stop_loss_price"], 10.5, places=4)

        self.pm.update_market_prices({"sh600598": 10.4})
        positions = self.pm.db.get_positions()
        should_sell, reason = self.pm.check_exit_conditions(positions[0])
        self.assertTrue(should_sell)
        self.assertEqual(reason, "Trailing Stop")

    def test_partial_take_profit_uses_100_share_lots(self):
        """测试 +15% 强制减仓且卖出股数按 100 股整数倍"""
        self.pm.buy("sh600598", "北大荒", 10.0, 0.1, 11.5, 9.5, "Test")
        self._set_bought_at("sh600598", datetime.now() - timedelta(days=1, minutes=31))

        self.pm.update_market_prices({"sh600598": 11.6})
        actions = self.pm.process_exits(now=datetime.now())
        self.assertEqual(actions[0]["action"], "强制减仓")

        positions = self.pm.db.get_positions()
        self.assertEqual(positions[0]["quantity"], 500)
        self.assertTrue(positions[0]["partial_exit_done"])

    def test_sell_applies_stamp_duty_rate(self):
        """测试卖出手续费包含万五印花税"""
        initial_balance = self.pm.account["balance"]
        self.pm.buy("sh600598", "北大荒", 10.0, 0.1, 11.5, 9.5, "Test")
        self._set_bought_at("sh600598", datetime.now() - timedelta(days=1, minutes=31))

        success = self.pm.sell("sh600598", 10.0, "Manual Exit")
        self.assertTrue(success)

        expected_buy_cost = 10000.0 + 5.0
        expected_sell_revenue = 10000.0 - 5.0 - 5.0
        expected_balance = initial_balance - expected_buy_cost + expected_sell_revenue
        self.assertAlmostEqual(self.pm.account["balance"], expected_balance, places=4)

if __name__ == '__main__':
    unittest.main()
