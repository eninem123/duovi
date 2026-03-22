import asyncio
import gc
import json
import os
import re
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


def setup_logging(log_file="logs/runtime.log"):
    """控制台实时输出 + 文件落盘日志"""
    fmt = "%(asctime)s - %(levelname)s - %(message)s"
    formatter = logging.Formatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)

    if log_file:
        os.makedirs(os.path.dirname(log_file), exist_ok=True)
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)


def run_async(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        result = loop.run_until_complete(coro)
        loop.run_until_complete(asyncio.sleep(0))
        if sys.platform.startswith("win"):
            gc.collect()
        return result
    finally:
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        except Exception:
            pass
        try:
            loop.run_until_complete(loop.shutdown_default_executor())
        except Exception:
            pass
        asyncio.set_event_loop(None)
        if sys.platform.startswith("win"):
            gc.collect()
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

    def is_market_open(self):
        """检查当前是否为 A 股交易时间（9:30-11:30, 13:00-15:00）"""
        now = datetime.datetime.now()
        # 排除周末
        if now.weekday() >= 5:
            return False
        
        current_time = now.time()
        morning_start = datetime.time(9, 30)
        morning_end = datetime.time(11, 30)
        afternoon_start = datetime.time(13, 0)
        afternoon_end = datetime.time(15, 0)
        
        return (morning_start <= current_time <= morning_end) or \
               (afternoon_start <= current_time <= afternoon_end)

    def _summarize_lock_status(self, positions):
        if not positions:
            return "已解锁"
        locked_lines = []
        for position in positions:
            status = self.portfolio.get_lock_status(position)
            if status["is_locked"]:
                locked_lines.append(f"{position['symbol']} 锁定中({status['remaining_minutes']}min)")
        return " / ".join(locked_lines) if locked_lines else "已解锁"

    def _build_risk_text(self, positions):
        if not positions:
            return "空仓，暂无止损/止盈距离"

        position = positions[0]
        avg_price = float(position["avg_price"])
        current_price = float(position["current_price"])
        stop_loss_pct = abs(float(self.trading_config.get("stop_loss", -0.05)))
        partial_take_at_return = float(self.trading_config.get("partial_take_at_return", 0.15))
        stop_line = max(
            avg_price * (1 - stop_loss_pct),
            float(position.get("stop_loss_price") or 0.0),
        )
        take_line = avg_price * (1 + partial_take_at_return)
        stop_distance = ((current_price / stop_line) - 1) * 100 if stop_line else 0.0
        take_distance = ((take_line / current_price) - 1) * 100 if current_price else 0.0
        return f"距离止损点 {stop_distance:+.2f}% / 距离止盈点 {take_distance:+.2f}%"

    def _extract_thinking_trace(self, decision):
        thinking_trace = decision.get("thinking_trace") or {}
        if isinstance(thinking_trace, list):
            return {
                "data_arch": thinking_trace[0] if len(thinking_trace) > 0 else "",
                "notebooklm": thinking_trace[1] if len(thinking_trace) > 1 else "",
                "game_psych": thinking_trace[2] if len(thinking_trace) > 2 else "",
                "trend": thinking_trace[3] if len(thinking_trace) > 3 else "",
            }
        if isinstance(thinking_trace, dict):
            return {
                "data_arch": thinking_trace.get("data_arch") or thinking_trace.get("architect") or "",
                "notebooklm": thinking_trace.get("notebooklm") or thinking_trace.get("knowledge") or "",
                "game_psych": thinking_trace.get("game_psych") or thinking_trace.get("psychology") or "",
                "trend": thinking_trace.get("trend") or thinking_trace.get("momentum") or "",
            }
        return {"data_arch": "", "notebooklm": "", "game_psych": "", "trend": ""}

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
            "risk_text": self._build_risk_text(positions),
            "lock_status": self._summarize_lock_status(positions),
            "thinking_trace": self._extract_thinking_trace(decision),
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
        """执行 NotebookLM 返回的持仓卖出/减仓建议（受置信度与 can_sell 门禁）。"""
        actions = []
        if not eval_payload:
            return actions
        cfg = (self.config.get("agent") or {}).get("exit_review") or {}
        if not cfg.get("enabled", True):
            return actions
        try:
            min_conf = float(cfg.get("min_confidence", 0.6))
        except (TypeError, ValueError):
            min_conf = 0.6

        default_partial = float(self.trading_config.get("partial_take_ratio", 0.5))
        for ev in eval_payload.get("evaluations") or []:
            if not isinstance(ev, dict):
                continue
            sym = str(ev.get("symbol") or "").strip()
            if not sym:
                continue
            positions = self.portfolio.db.get_positions()
            pos_by_sym = {p["symbol"]: p for p in positions}
            resolved = self._match_holdings_symbol(sym, pos_by_sym)
            if not resolved:
                continue
            pos = pos_by_sym[resolved]
            sym = resolved
            try:
                conf = float(ev.get("confidence", 0.0) or 0.0)
            except (TypeError, ValueError):
                conf = 0.0
            if conf < min_conf:
                logging.info(f"[{sym}] 智能体退出建议置信度 {conf:.2f} < {min_conf:.2f}，不执行。")
                continue
            action = MCPAgent.normalize_exit_action(ev.get("action"))
            if action == "hold":
                continue
            if not self.portfolio.can_sell(pos):
                logging.info(f"[{sym}] 智能体建议 {action}，但 T+1/锁仓未满足，跳过执行。")
                continue
            reason = f"智能体退出复核: {ev.get('reason') or action}"
            if action == "sell":
                if self.portfolio.sell(sym, pos["current_price"], reason):
                    actions.append({"action": "卖出", "symbol": sym, "name": pos.get("name"), "reason": reason})
            elif action == "partial":
                try:
                    frac = float(ev.get("partial_ratio", default_partial) or default_partial)
                except (TypeError, ValueError):
                    frac = default_partial
                frac = max(0.01, min(1.0, frac))
                if self.portfolio.sell_partial(sym, pos["current_price"], frac, reason):
                    actions.append({"action": "智能体减仓", "symbol": sym, "name": pos.get("name"), "reason": reason})
        return actions

    @staticmethod
    def _match_holdings_symbol(raw, pos_by_sym):
        if raw in pos_by_sym:
            return raw
        compact = re.sub(r"[^0-9]", "", raw or "")
        if len(compact) == 6:
            for k in pos_by_sym:
                if re.sub(r"[^0-9]", "", k) == compact:
                    return k
        return None

    async def run_decision_cycle(self, force=False):
        """单次定时决策循环"""
        if not force and not self.is_market_open():
            logging.info("当前非 A 股交易时段，系统休眠中...")
            snapshot = self._record_mda_snapshot(reason="当前非交易时段，系统保持观望。")
            self.reporter.generate_html_dashboard()
            feishu_notify.notify_decision_cycle(snapshot, [])
            return snapshot

        logging.info("--- 开始新的交易决策周期 ---")
        self.portfolio.print_status()
        cycle_events = []

        # 1. 更新当前持仓价格并检查是否需要止损/止盈/到期卖出
        positions = self.portfolio.db.get_positions()
        symbols = [p["symbol"] for p in positions]
        
        if symbols:
            logging.info(f"正在更新持仓价格: {symbols}")
            price_dict = await self.agent.update_holdings_prices(symbols)
            if price_dict:
                self.portfolio.update_market_prices(price_dict)

            positions = self.portfolio.db.get_positions()
            er = (self.config.get("agent") or {}).get("exit_review") or {}
            if er.get("enabled", True) and positions:
                logging.info("智能体持仓退出复核（NotebookLM + 行情 MCP）...")
                position_gates = {}
                for p in positions:
                    ls = self.portfolio.get_lock_status(p)
                    position_gates[p["symbol"]] = {
                        "can_sell": self.portfolio.can_sell(p),
                        "is_locked": ls["is_locked"],
                        "lock_remaining_minutes": ls["remaining_minutes"],
                    }
                market_ctx = await self.agent.get_market_data()
                eval_result = await self.agent.evaluate_position_exits(
                    positions,
                    self.portfolio.account,
                    market_ctx or {},
                    self.trading_config,
                    position_gates=position_gates,
                )
                cycle_events.extend(self._apply_agent_exit_reviews(eval_result))

            positions = self.portfolio.db.get_positions()
            cycle_events.extend(self.portfolio.process_exits())

        positions = self.portfolio.db.get_positions()
        symbols = [p["symbol"] for p in positions]

        # 2. 检查仓位限制：最大允许同时持仓 3 只股票，保证资金集中度
        max_pos = self.risk_gate.max_positions()
        if len(symbols) >= max_pos:
            logging.info("当前持仓已达上限(%s只)，跳过买入决策。", max_pos)
            snapshot = self._record_mda_snapshot(
                action=cycle_events[-1]["action"] if cycle_events else "观望",
                symbol=cycle_events[-1]["symbol"] if cycle_events else None,
                name=cycle_events[-1]["name"] if cycle_events else None,
                reason=cycle_events[-1]["reason"] if cycle_events else f"当前持仓已达上限({max_pos}只)，本轮只做风控，不新增买入。",
            )
            self.reporter.generate_html_dashboard()
            feishu_notify.notify_decision_cycle(snapshot, cycle_events)
            logging.info("--- 交易决策周期结束 ---\n")
            return snapshot

        # 3. 如果还有闲置资金，则寻找新的买入机会
        account = self.portfolio.account
        final_snapshot = None
        min_cash_ratio = self.risk_gate.min_cash_ratio_to_scan()
        if account["balance"] > account["total_assets"] * min_cash_ratio:
            logging.info(
                "资金充足且满足风控条件（可用现金 > 总资产×%.0f%%），开始扫描买入信号...",
                min_cash_ratio * 100,
            )

            two_stage = (self.config.get("agent") or {}).get("two_stage_screening") or {}
            decision = None
            prompt = ""
            raw_response = None
            if two_stage.get("enabled"):
                ts_result = await self.agent.run_two_stage_buy_decision()
                if ts_result:
                    decision, prompt, raw_response = ts_result
                else:
                    logging.info("两阶段选股未完成，回退单阶段 get_market_data + make_decision。")

            if decision is None:
                market_data = await self.agent.get_market_data()
                if market_data:
                    decision, prompt, raw_response = await self.agent.make_decision(market_data)
                else:
                    logging.warning("未能获取市场数据，跳过买入扫描。")

            if decision:
                symbol = str(decision.get("symbol") or "").strip()
                if symbol.lower() == "null":
                    symbol = ""
                win_rate = float(decision.get("win_rate_confidence", 0.0) or 0.0)
                threshold = float(self.trading_config.get("win_rate_threshold", 0.75))

                self.portfolio.db.log_decision(
                    prompt=prompt,
                    kb_quote=decision.get("reason", ""),
                    logic=json.dumps(decision, ensure_ascii=False),
                    raw_response=raw_response
                )

                action = "观望"
                reason = decision.get("reason", "")
                name = decision.get("name", "Unknown")
                if not decision.get("success", True):
                    logging.warning(f"MDA 决策降级：{decision.get('error') or reason}")

                if symbol:
                    if win_rate < threshold:
                        reason = (
                            f"智能体找到了标的 {symbol}，但综合胜率仅 {win_rate*100:.1f}%，"
                            f"未达到 {threshold*100:.1f}% 的执行阈值。"
                        )
                        logging.warning(reason)
                    else:
                        block_reason = self.risk_gate.buy_blocked_reason(
                            decision, len(self.portfolio.db.get_positions())
                        )
                        if block_reason:
                            reason = block_reason
                            logging.warning(block_reason)
                        else:
                            price_dict = await self.agent.update_holdings_prices([symbol])
                            current_price = price_dict.get(symbol)
                            if current_price:
                                logging.info(
                                    f"🎯 触发高胜率信号({win_rate*100:.1f}%)! 准备买入: {symbol} - {name}"
                                )
                                success = self.portfolio.buy(
                                    symbol=symbol,
                                    name=name,
                                    price=current_price,
                                    position_pct=decision.get("position_pct", 0.3),
                                    target_price=decision.get("target_price", current_price * 1.15),
                                    stop_loss_price=max(
                                        decision.get("stop_loss_price", current_price * 0.95),
                                        current_price * (1 - abs(float(self.trading_config.get("stop_loss", -0.05))))
                                    ),
                                    reason=decision.get("reason", "Agent Signal")
                                )
                                if success:
                                    action = "买入"
                                    reason = decision.get("reason", "触发 MDA 高胜率买入。")
                                    if isinstance(decision, dict):
                                        ea = decision.setdefault("execution_audit", {})
                                        ea["price_at_execution"] = float(current_price)
                                        for p in self.portfolio.db.get_positions():
                                            if p["symbol"] == symbol:
                                                ea["executed_quantity"] = int(p["quantity"])
                                                ea["notional"] = float(p["quantity"]) * float(current_price)
                                                break
                            else:
                                reason = f"未能获取推荐股票 {symbol} 的实时价格。"
                                logging.warning(reason)
                else:
                    reason = decision.get("reason", "当前市场无符合阈值的标的，保持观望。")
                    logging.info("智能体认为当前市场环境不满足买入条件，保持观望。")

                if cycle_events and action == "观望":
                    latest_event = cycle_events[-1]
                    action = latest_event["action"]
                    reason = f"{latest_event['reason']}；{reason}" if reason else latest_event["reason"]
                    symbol = latest_event["symbol"]
                    name = latest_event["name"]
                final_snapshot = self._record_mda_snapshot(
                    decision=decision,
                    action=action,
                    symbol=symbol or None,
                    name=name,
                    reason=reason,
                )
            else:
                logging.warning("智能体未能返回有效决策。")
                final_snapshot = self._record_mda_snapshot(
                    action=cycle_events[-1]["action"] if cycle_events else "观望",
                    symbol=cycle_events[-1]["symbol"] if cycle_events else None,
                    name=cycle_events[-1]["name"] if cycle_events else None,
                    reason=cycle_events[-1]["reason"] if cycle_events else "智能体未能返回有效决策。",
                )
        else:
            logging.info(
                "可用资金不足总资产×%.0f%%，本轮跳过买入扫描。",
                min_cash_ratio * 100,
            )
            final_snapshot = self._record_mda_snapshot(
                action=cycle_events[-1]["action"] if cycle_events else "观望",
                symbol=cycle_events[-1]["symbol"] if cycle_events else None,
                name=cycle_events[-1]["name"] if cycle_events else None,
                reason=cycle_events[-1]["reason"] if cycle_events else f"可用资金不足总资产×{min_cash_ratio*100:.0f}%，本轮仅做风控观察。",
            )

        if not final_snapshot:
            final_snapshot = self._record_mda_snapshot(
                action=cycle_events[-1]["action"] if cycle_events else "观望",
                symbol=cycle_events[-1]["symbol"] if cycle_events else None,
                name=cycle_events[-1]["name"] if cycle_events else None,
                reason=cycle_events[-1]["reason"] if cycle_events else "本轮未触发买入条件。",
            )

        self.reporter.generate_html_dashboard()
        feishu_notify.notify_decision_cycle(final_snapshot, cycle_events)
        logging.info("--- 交易决策周期结束 ---\n")
        return final_snapshot

    def run_tick(self):
        """为 schedule 封装同步方法"""
        run_async(self.run_decision_cycle())

    def run_manual_refresh(self):
        """手动执行一轮完整决策逻辑"""
        return run_async(self.run_decision_cycle(force=True))

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


async def test_notebooklm_once(config_path="config.yaml"):
    """快速验证 notebooklm 是否可用"""
    agent = MCPAgent(config_path=config_path)
    decision, _, raw_response = await agent.make_decision("请用一句话概述该知识库核心主题。")
    if raw_response:
        logging.info("NotebookLM 连通性测试通过（已返回内容）。")
    else:
        logging.warning("NotebookLM 连通性测试失败（未返回内容）。")
    return decision, raw_response


def parse_args():
    parser = argparse.ArgumentParser(description="A股量化模拟交易系统")
    parser.add_argument("--mode", choices=["live", "backtest", "chat"], help="运行模式，覆盖 config.yaml 配置")
    parser.add_argument("--interval", type=int, default=5, help="live 模式的决策间隔（分钟）")
    parser.add_argument("--test-notebooklm", action="store_true", help="启动前执行一次 NotebookLM 连通性测试")
    parser.add_argument(
        "--probe-keys",
        action="store_true",
        help="启动前轻量探测 Tushare / Tavily / LiteLLM 是否可用（结果写入日志）",
    )
    parser.add_argument("--log-file", default="logs/runtime.log", help="日志文件路径；为空则仅控制台输出")
    return parser.parse_args()


def start_chat_mode():
    """NotebookLM 持续问答模式"""
    agent = MCPAgent(config_path="config.yaml")
    history = []
    print("进入 NotebookLM 持续问答模式（输入 exit 退出）")
    while True:
        question = input("\n你> ").strip()
        if not question:
            continue
        if question.lower() in {"exit", "quit", "q"}:
            print("已退出问答模式。")
            break
        result = run_async(agent.ask_multi_domain_foresight(question, history=history))
        answer = result.get("final_answer") if result else None
        if not answer:
            answer = "multi-domain-foresight 未生成有效结果，请稍后重试。"
        print(f"\n助手> {answer}\n")
        history.append({"q": question, "a": answer})

def _load_local_env():
    base = os.path.dirname(os.path.abspath(__file__))
    override = (os.environ.get("QUANT_SIM_ENV_FILE") or "").strip()
    env_path = override if override and os.path.isfile(override) else os.path.join(base, ".env")
    if not os.path.isfile(env_path):
        return
    try:
        from dotenv import load_dotenv

        load_dotenv(env_path)
    except ImportError:
        pass


if __name__ == "__main__":
    _load_local_env()
    args = parse_args()
    setup_logging(log_file=args.log_file if args.log_file else None)
    system = QuantTradingSystem()
    exit_code = 0
    try:
        runtime_mode = args.mode or system.config.get("runtime", {}).get("mode", "live")

        if args.test_notebooklm:
            run_async(test_notebooklm_once())

        if args.probe_keys:
            from key_probe import run_key_probe

            run_async(run_key_probe(system.config))

        if runtime_mode == "chat":
            start_chat_mode()
        elif runtime_mode == "backtest":
            logging.info("当前运行模式: 历史回测")
            backtester = HistoricalBacktester(system.config)
            backtester.run()
            system.reporter.generate_report()
            system.reporter.generate_html_dashboard()
        else:
            system.start(interval_minutes=args.interval)
    except KeyboardInterrupt:
        logging.info("系统收到停止信号，正在退出...")
    except Exception:
        exit_code = 1
        logging.exception("系统异常退出")
    finally:
        try:
            logging.shutdown()
        finally:
            try:
                sys.stdout.flush()
                sys.stderr.flush()
            finally:
                os._exit(exit_code)
