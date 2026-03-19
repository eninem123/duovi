import asyncio
import schedule
import time
import logging
from portfolio import PortfolioManager
from mcp_agent import MCPAgent

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

class QuantTradingSystem:
    def __init__(self):
        self.portfolio = PortfolioManager()
        self.agent = MCPAgent()
        self.is_running = False

    async def _trading_tick(self):
        """单次定时决策循环"""
        logging.info("--- 开始新的交易决策周期 ---")
        self.portfolio.print_status()

        # 1. 更新当前持仓价格并检查是否需要止损/止盈/到期卖出
        positions = self.portfolio.db.get_positions()
        symbols = [p["symbol"] for p in positions]
        
        if symbols:
            logging.info(f"正在更新持仓价格: {symbols}")
            price_dict = await self.agent.update_holdings_prices(symbols)
            if price_dict:
                self.portfolio.update_market_prices(price_dict)
                self.portfolio.process_exits()
        
        # 2. 如果还有闲置资金（比如超过总资产的10%），则寻找新的买入机会
        account = self.portfolio.account
        if account["balance"] > account["total_assets"] * 0.1:
            logging.info("资金充足，开始扫描市场寻找买入信号...")
            
            # 获取当前市场数据
            market_data = await self.agent.get_market_data()
            if market_data:
                # 让智能体做决策
                decision, prompt, raw_response = await self.agent.make_decision(market_data)
                
                if decision:
                    symbol = decision.get("symbol")
                    if symbol:
                        # 记录决策日志
                        self.portfolio.db.log_decision(
                            prompt=prompt,
                            kb_quote=decision.get("reason", ""),
                            logic=str(decision),
                            raw_response=raw_response
                        )
                        
                        # 尝试获取该股票的当前真实价格
                        price_dict = await self.agent.update_holdings_prices([symbol])
                        current_price = price_dict.get(symbol)
                        
                        if current_price:
                            # 挂单买入
                            logging.info(f"智能体推荐买入: {symbol} - {decision.get('name')}")
                            self.portfolio.buy(
                                symbol=symbol,
                                name=decision.get("name", "Unknown"),
                                price=current_price,
                                position_pct=decision.get("position_pct", 0.2),
                                target_price=decision.get("target_price", current_price * 1.15),
                                stop_loss_price=decision.get("stop_loss_price", current_price * 0.95),
                                reason=decision.get("reason", "Agent Signal")
                            )
                        else:
                            logging.warning(f"未能获取推荐股票 {symbol} 的实时价格。")
                    else:
                        logging.info("智能体认为当前无符合条件的股票。")
                else:
                    logging.warning("智能体未能返回有效决策。")
        else:
            logging.info("资金利用率较高，本轮跳过买入扫描。")
            
        logging.info("--- 交易决策周期结束 ---\n")

    def run_tick(self):
        """为 schedule 封装同步方法"""
        asyncio.run(self._trading_tick())

    def start(self, interval_minutes=5):
        """启动定时调度"""
        logging.info(f"启动 A 股量化模拟交易系统，决策间隔: {interval_minutes} 分钟")
        
        # 立即执行一次
        self.run_tick()
        
        # 设置定时任务
        schedule.every(interval_minutes).minutes.do(self.run_tick)
        
        self.is_running = True
        while self.is_running:
            schedule.run_pending()
            time.sleep(1)

if __name__ == "__main__":
    # 可以通过修改配置文件来改变行为
    system = QuantTradingSystem()
    try:
        # 注意：这里设定为每5分钟执行一次
        system.start(interval_minutes=5)
    except KeyboardInterrupt:
        logging.info("系统收到停止信号，正在退出...")
