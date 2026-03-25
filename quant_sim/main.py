import asyncio
import gc
import json
import os
import schedule
import time
import datetime
import logging
import argparse
import sys
import yaml
from portfolio import PortfolioManager
from mcp_agent import MCPAgent
from report import ReportGenerator
from risk_gate import RiskGate
from backtest import HistoricalBacktester
import feishu_notify
import utils

def setup_logging(log_file="logs/runtime.log"):
    """控制台实时输出 + 文件落盘日志"""
    fmt = "%(asctime)s - %(levelname)s - %(message)s"
    formatter = logging.Formatter(fmt)
    root = logging.getLogger()
    
    # 避免重复添加 handler
    if not root.handlers:
        root.setLevel(logging.INFO)
        console = logging.StreamHandler()
        console.setFormatter(formatter)
        root.addHandler(console)

        if log_file:
            os.makedirs(os.path.dirname(log_file), exist_ok=True)
            file_handler = logging.FileHandler(log_file, encoding="utf-8")
            file_handler.setFormatter(formatter)
            root.addHandler(file_handler)

def run_async(coro):
    """标准的异步运行包装器"""
    try:
        return asyncio.run(coro)
    except Exception as e:
        logging.error(f"Async execution error: {e}")
        # 降级处理
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

class QuantTradingSystem:
    def __init__(self):
        with open("config.yaml", "r", encoding="utf-8") as f:
            self.config = yaml.safe_load(f)
        self.trading_config = self.config.get("trading", {})
        self.portfolio = PortfolioManager()
        self.agent = MCPAgent()
        self.reporter = ReportGenerator()
        self.risk_gate = RiskGate(self.config)
        self.is_running = False

    def _summarize_lock_status(self, positions):
        if not positions:
            return "已解锁"
        locked_lines = []
        for position in positions:
            status = self.portfolio.get_lock_status(position)
            if status["is_locked"]:
                locked_lines.append(f"{position['symbol']} 锁定中({status['remaining_minutes']}min)")
        return " / ".join(locked_lines) if locked_lines else "已解锁"

    def _record_mda_snapshot(self, decision=None, action="观望", symbol=None, name=None, reason=""):
        decision = decision or {}
        positions = self.portfolio.db.get_positions()
        self.portfolio.sync_total_assets()
        
        snapshot = {
            "action": action,
            "symbol": symbol or decision.get("symbol"),
            "name": name or decision.get("name"),
            "total_score": float(decision.get("total_score", 0.0) or 0.0),
            "win_rate_confidence": float(decision.get("win_rate_confidence", 0.0) or 0.0),
            "dimension_scores": decision.get("dimension_scores") or {},
            "risk_text": utils.build_risk_text(positions[0] if positions else None, self.trading_config),
            "lock_status": self._summarize_lock_status(positions),
            "thinking_trace": utils.extract_thinking_trace(decision),
            "logic": decision,
            "reason": reason or decision.get("reason", ""),
        }
        
        self.portfolio.db.log_mda_snapshot(
            action=snapshot["action"] or "观望",
            symbol=snapshot["symbol"],
            name=snapshot["name"],
            total_score=snapshot["total_score"],
            win_rate_confidence=snapshot["win_rate_confidence"],
            dimension_scores=snapshot["dimension_scores"],
            risk_text=snapshot["risk_text"],
            lock_status=snapshot["lock_status"],
            thinking_trace=snapshot["thinking_trace"],
            logic=snapshot["logic"],
            reason=snapshot["reason"],
        )
        return snapshot

    def _apply_agent_exit_reviews(self, eval_payload):
        """执行智能体建议的退出动作"""
        actions = []
        if not eval_payload:
            return actions
            
        cfg = (self.config.get("agent") or {}).get("exit_review", {})
        if not cfg.get("enabled", True):
            return actions
            
        min_conf = float(cfg.get("min_confidence", 0.6))
        default_partial = float(self.trading_config.get("partial_take_ratio", 0.5))
        
        for ev in eval_payload.get("evaluations", []):
            if not isinstance(ev, dict): continue
            
            sym = str(ev.get("symbol") or "").strip()
            if not sym: continue
            
            positions = self.portfolio.db.get_positions()
            pos_by_sym = {p["symbol"]: p for p in positions}
            resolved = utils.match_holdings_symbol(sym, pos_by_sym)
            if not resolved: continue
            
            pos = pos_by_sym[resolved]
            conf = float(ev.get("confidence", 0.0) or 0.0)
            
            if conf < min_conf:
                logging.info(f"[{resolved}] 退出建议置信度 {conf:.2f} < {min_conf:.2f}，不执行。")
                continue
                
            action = MCPAgent.normalize_exit_action(ev.get("action"))
            if action == "hold": continue
            
            if not self.portfolio.can_sell(pos):
                logging.info(f"[{resolved}] 智能体建议 {action}，但 T+1/锁仓未满足，跳过。")
                continue
                
            reason = f"智能体退出复核: {ev.get('reason') or action}"
            if action == "sell":
                if self.portfolio.sell(resolved, pos["current_price"], reason):
                    actions.append({"action": "卖出", "symbol": resolved, "name": pos.get("name"), "reason": reason})
            elif action == "partial":
                frac = max(0.01, min(1.0, float(ev.get("partial_ratio", default_partial))))
                if self.portfolio.sell_partial(resolved, pos["current_price"], frac, reason):
                    actions.append({"action": "智能体减仓", "symbol": resolved, "name": pos.get("name"), "reason": reason})
        return actions

    async def run_decision_cycle(self, force=False):
        """单次决策循环核心逻辑"""
        if not force and not utils.is_market_open():
            logging.info("当前非 A 股交易时段，系统休眠中...")
            snapshot = self._record_mda_snapshot(reason="当前非交易时段，系统保持观望。")
            self.reporter.generate_html_dashboard()
            feishu_notify.notify_decision_cycle(snapshot, [])
            return snapshot

        logging.info("--- 开始新的交易决策周期 ---")
        self.portfolio.print_status()
        cycle_events = []

        # 1. 更新持仓价格并处理规则止损/止盈
        positions = self.portfolio.db.get_positions()
        if positions:
            symbols = [p["symbol"] for p in positions]
            logging.info(f"正在更新持仓价格: {symbols}")
            price_dict = await self.agent.update_holdings_prices(symbols)
            if price_dict:
                self.portfolio.update_market_prices(price_dict)
            
            # 规则退出
            rule_exits = self.portfolio.process_exits()
            cycle_events.extend(rule_exits)
            
            # 智能体退出复核
            updated_positions = self.portfolio.db.get_positions()
            if updated_positions:
                eval_payload = await self.agent.review_holdings_exit(updated_positions)
                agent_exits = self._apply_agent_exit_reviews(eval_payload)
                cycle_events.extend(agent_exits)

        # 2. 检查开仓机会
        current_positions = self.portfolio.db.get_positions()
        blocked_reason = self.risk_gate.buy_blocked_reason({}, len(current_positions))
        
        if blocked_reason:
            logging.info(f"开仓门禁拦截: {blocked_reason}")
            snapshot = self._record_mda_snapshot(reason=f"开仓门禁拦截: {blocked_reason}")
        else:
            logging.info("开始扫描市场机会...")
            decision = await self.agent.make_decision()
            
            if decision.get("action") == "buy":
                # 再次校验风险门禁
                blocked_reason = self.risk_gate.buy_blocked_reason(decision, len(current_positions))
                if not blocked_reason:
                    success = self.portfolio.buy(
                        symbol=decision["symbol"],
                        name=decision["name"],
                        price=decision["current_price"],
                        position_pct=decision.get("position_pct", 0.3),
                        target_price=decision.get("target_price"),
                        stop_loss_price=decision.get("stop_loss_price"),
                        reason=decision.get("reason", "智能体建议买入")
                    )
                    if success:
                        cycle_events.append({"action": "买入", "symbol": decision["symbol"], "name": decision["name"], "reason": decision.get("reason")})
                        snapshot = self._record_mda_snapshot(decision, action="买入")
                    else:
                        snapshot = self._record_mda_snapshot(decision, reason="买入执行失败（资金不足或已持仓）")
                else:
                    logging.info(f"决策后拦截: {blocked_reason}")
                    snapshot = self._record_mda_snapshot(decision, reason=f"决策后拦截: {blocked_reason}")
            else:
                snapshot = self._record_mda_snapshot(decision, action="观望", reason=decision.get("reason", "未发现合适机会"))

        # 3. 生成报告并通知
        self.reporter.generate_html_dashboard()
        feishu_notify.notify_decision_cycle(snapshot, cycle_events)
        logging.info("--- 决策周期结束 ---")
        return snapshot

    def start(self, interval_minutes=30):
        self.is_running = True
        logging.info(f"系统启动，决策周期: {interval_minutes} 分钟")
        
        # 初始运行一次
        run_async(self.run_decision_cycle())
        
        schedule.every(interval_minutes).minutes.do(lambda: run_async(self.run_decision_cycle()))
        
        try:
            while self.is_running:
                schedule.run_pending()
                time.sleep(1)
        except KeyboardInterrupt:
            self.stop()

    def stop(self):
        self.is_running = False
        logging.info("系统停止中...")

def main():
    parser = argparse.ArgumentParser(description="Quant Trading System")
    parser.add_argument("--mode", choices=["live", "backtest"], help="运行模式")
    parser.add_argument("--once", action="store_true", help="仅运行一次决策周期")
    parser.add_argument("--interval", type=int, default=30, help="决策周期分钟数")
    parser.add_argument("--reset", action="store_true", help="重置模拟数据库")
    parser.add_argument("--probe-keys", action="store_true", help="检查 API 密钥连通性")
    args = parser.parse_args()

    setup_logging()

    if args.probe_keys:
        from key_probe import probe_all
        probe_all()
        return

    system = QuantTradingSystem()

    if args.reset:
        confirm = input("确定要重置数据库吗？所有交易记录将丢失 (y/n): ")
        if confirm.lower() == 'y':
            system.portfolio.db.reset_simulation(system.trading_config.get("initial_capital", 100000.0))
            logging.info("数据库已重置")
        return

    mode = args.mode or system.config.get("runtime", {}).get("mode", "live")
    
    if mode == "backtest":
        logging.info("启动历史回测模式...")
        tester = HistoricalBacktester()
        tester.run()
    else:
        if args.once:
            run_async(system.run_decision_cycle(force=True))
        else:
            system.start(interval_minutes=args.interval)

if __name__ == "__main__":
    main()
