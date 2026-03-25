import datetime
import re

def is_market_open():
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
    
    # 基础时间检查
    in_trading_hours = (morning_start <= current_time <= morning_end) or \
                       (afternoon_start <= current_time <= afternoon_end)
    
    # TODO: 这里可以加入节假日检查逻辑
    # 简单示例：如果是 2026 年的春节/国庆等，返回 False
    
    return in_trading_hours

def extract_thinking_trace(decision):
    """从决策 JSON 中提取思维链"""
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

def build_risk_text(position, trading_config):
    """根据持仓计算止损/止盈距离文本"""
    if not position:
        return "空仓，暂无止损/止盈距离"

    avg_price = float(position["avg_price"])
    current_price = float(position["current_price"])
    stop_loss_pct = abs(float(trading_config.get("stop_loss", -0.05)))
    partial_take_at_return = float(trading_config.get("partial_take_at_return", 0.15))
    
    stop_line = max(
        avg_price * (1 - stop_loss_pct),
        float(position.get("stop_loss_price") or 0.0),
    )
    take_line = avg_price * (1 + partial_take_at_return)
    
    stop_distance = ((current_price / stop_line) - 1) * 100 if stop_line else 0.0
    take_distance = ((take_line / current_price) - 1) * 100 if current_price else 0.0
    
    return f"距离止损点 {stop_distance:+.2f}% / 距离止盈点 {take_distance:+.2f}%"

def match_holdings_symbol(raw, pos_by_sym):
    """匹配持仓代码，支持模糊匹配"""
    if raw in pos_by_sym:
        return raw
    compact = re.sub(r"[^0-9]", "", raw or "")
    if len(compact) == 6:
        for k in pos_by_sym:
            if re.sub(r"[^0-9]", "", k) == compact:
                return k
    return None
