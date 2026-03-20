import unittest
import os
import sqlite3
from datetime import datetime, timedelta
from portfolio import PortfolioManager

class TestTradingSystem(unittest.TestCase):
    def setUp(self):
        # 使用测试数据库
        self.db_path = "test_quant_sim.db"
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

    def test_buy_calculation(self):
        """测试买入逻辑及手续费计算精度"""
        initial_balance = self.pm.account["balance"]
        
        # 尝试买入：价格 10.0，仓位 10% (100,000元)
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
        
        # 验证数量 (100000 / 10 = 10000股)
        positions = self.pm.db.get_positions()
        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0]["quantity"], 10000)
        
        # 验证手续费和余额 (10000 * 10 = 100000, 佣金 = max(100000 * 0.0003, 5.0) = 30.0)
        expected_cost = 100000.0 + 30.0
        expected_balance = initial_balance - expected_cost
        
        self.assertAlmostEqual(self.pm.account["balance"], expected_balance, places=4)

    def test_lock_period(self):
        """测试1小时锁仓限制"""
        self.pm.buy("sh600598", "北大荒", 10.0, 0.1, 11.5, 9.5, "Test")
        positions = self.pm.db.get_positions()
        
        # 刚刚买入，不应该能卖出
        can_sell = self.pm.can_sell(positions[0])
        self.assertFalse(can_sell)
        
        # 修改数据库中的 bought_at 时间为1天前 (满足 T+1 和 1小时锁仓)
        old_time = (datetime.now() - timedelta(days=1)).isoformat()
        with self.pm.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE positions SET bought_at = ? WHERE symbol = ?", (old_time, "sh600598"))
            conn.commit()
            
        positions = self.pm.db.get_positions()
        can_sell = self.pm.can_sell(positions[0])
        self.assertTrue(can_sell)

    def test_stop_loss(self):
        """测试止损触发逻辑"""
        self.pm.buy("sh600598", "北大荒", 10.0, 0.1, 11.5, 9.5, "Test")
        
        # 修改为已过锁定期 (模拟 T+1)
        old_time = (datetime.now() - timedelta(days=1)).isoformat()
        with self.pm.db.get_connection() as conn:
            conn.execute("UPDATE positions SET bought_at = ? WHERE symbol = ?", (old_time, "sh600598"))
            conn.commit()
            
        # 价格跌至 9.4 (低于止损价 9.5)
        self.pm.update_market_prices({"sh600598": 9.4})
        
        positions = self.pm.db.get_positions()
        should_sell, reason = self.pm.check_exit_conditions(positions[0])
        self.assertTrue(should_sell)
        self.assertEqual(reason, "Stop Loss")

if __name__ == '__main__':
    unittest.main()
