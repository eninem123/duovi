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
        self.last_buy_time = 0  # 记录上一次买入时间的时间戳

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
        
        # 2. 检查买入冷却期 (例如距离上次买入至少间隔 4 小时 = 14400秒，防止高频建仓)
        current_time = time.time()
        cooldown_seconds = 4 * 3600
        if current_time - self.last_buy_time < cooldown_seconds:
            logging.info(f"当前处于买入冷却期内 (还剩 {(cooldown_seconds - (current_time - self.last_buy_time))/60:.1f} 分钟)，跳过买入决策。")
            logging.info("--- 交易决策周期结束 ---\n")
            return

        # 3. 检查仓位限制：最大允许同时持仓 3 只股票，保证资金集中度
        if len(symbols) >= 3:
            logging.info("当前持仓已达上限(3只)，跳过买入决策。")
            logging.info("--- 交易决策周期结束 ---\n")
            return

        # 4. 如果还有闲置资金，则寻找新的买入机会
        account = self.portfolio.account
        if account["balance"] > account["total_assets"] * 0.2:
            logging.info("资金充足且满足风控条件，开始扫描市场寻找极高胜率的买入信号...")
            
            # 获取当前市场数据（已包含行业板块与宏观指数）
            market_data = await self.agent.get_market_data()
            if market_data:
                # 让智能体做决策
                decision, prompt, raw_response = await self.agent.make_decision(market_data)
                
                if decision:
                    symbol = decision.get("symbol")
                    win_rate = decision.get("win_rate_confidence", 0.0)
                    
                    if symbol and symbol.strip() != "null":
                        # 核心风控拦截：胜率不达标坚决不买
                        if win_rate < 0.87:
                            logging.warning(f"智能体找到了标的 {symbol}，但预判胜率({win_rate*100:.1f}%)未达到 87% 的绝对狙击标准，放弃买入。")
                        else:
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
                                logging.info(f"🎯 触发极高胜率信号({win_rate*100:.1f}%)! 准备买入: {symbol} - {decision.get('name')}")
                                success = self.portfolio.buy(
                                    symbol=symbol,
                                    name=decision.get("name", "Unknown"),
                                    price=current_price,
                                    position_pct=decision.get("position_pct", 0.3),
                                    target_price=decision.get("target_price", current_price * 1.15),
                                    stop_loss_price=decision.get("stop_loss_price", current_price * 0.95),
                                    reason=decision.get("reason", "Agent Signal")
                                )
                                if success:
                                    self.last_buy_time = time.time()  # 更新买入时间戳，触发冷却
                            else:
                                logging.warning(f"未能获取推荐股票 {symbol} 的实时价格。")
                    else:
                        logging.info("智能体认为当前市场环境恶劣，无符合 87% 胜率要求的股票，保持空仓。")
                else:
                    logging.warning("智能体未能返回有效决策。")
        else:
            logging.info("可用资金不足 20%，本轮跳过买入扫描。")
            
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
