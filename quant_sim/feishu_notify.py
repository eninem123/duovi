"""飞书自定义机器人：仅使用环境变量 FEISHU_WEBHOOK_URL。"""

from __future__ import annotations

import datetime
import json
import logging
import os
import urllib.error
import urllib.request

MAX_TEXT_LEN = 15000


def snapshot_to_feishu_text(snapshot: dict, cycle_events: list | None = None) -> str:
    lines = [
        f"【quant_sim 决策周期】{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"动作: {snapshot.get('action') or '观望'}",
    ]
    sym = snapshot.get("symbol")
    if sym:
        name = snapshot.get("name") or ""
        lines.append(f"标的: {sym} {name}".strip())
    wr = snapshot.get("win_rate_confidence")
    if wr is not None:
        try:
            lines.append(f"综合胜率(置信): {float(wr) * 100:.1f}%")
        except (TypeError, ValueError):
            pass
    ts = snapshot.get("total_score")
    if ts:
        try:
            if float(ts) != 0.0:
                lines.append(f"总分: {ts}")
        except (TypeError, ValueError):
            lines.append(f"总分: {ts}")
    ds = snapshot.get("dimension_scores") or {}
    if ds:
        parts = [f"{k}:{v}" for k, v in ds.items()]
        lines.append("维度得分: " + ", ".join(parts))
    reason = snapshot.get("reason") or ""
    if reason:
        lines.append(f"说明: {reason}")
    rt = snapshot.get("risk_text")
    if rt:
        lines.append(f"风险/持仓: {rt}")
    ls = snapshot.get("lock_status")
    if ls:
        lines.append(f"锁仓: {ls}")
    if cycle_events:
        lines.append("--- 本轮事件 ---")
        for ev in cycle_events:
            if isinstance(ev, dict):
                lines.append(
                    f"- {ev.get('action', '')} {ev.get('symbol', '')} {ev.get('name', '')}: {ev.get('reason', '')}"
                )
    return "\n".join(lines)[:MAX_TEXT_LEN]


def send_feishu_webhook_text(text: str) -> bool:
    url = (os.environ.get("FEISHU_WEBHOOK_URL") or "").strip()
    if not url or not (text or "").strip():
        return False
    payload = {"msg_type": "text", "content": {"text": text[:MAX_TEXT_LEN]}}
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        try:
            j = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            j = {}
        code = j.get("code")
        if code is not None and int(code) != 0:
            logging.warning("飞书 Webhook 业务错误: %s", raw[:800])
            return False
        sc = j.get("StatusCode")
        if sc is not None and int(sc) != 0:
            logging.warning("飞书 Webhook StatusCode: %s", raw[:800])
            return False
        return True
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace") if e.fp else ""
        logging.warning("飞书 Webhook HTTP 错误 %s: %s", e.code, body[:500])
        return False
    except Exception as e:
        logging.warning("飞书 Webhook 发送失败: %s", e)
        return False


def notify_decision_cycle(snapshot: dict, cycle_events: list | None = None) -> None:
    if not (os.environ.get("FEISHU_WEBHOOK_URL") or "").strip():
        return
    text = snapshot_to_feishu_text(snapshot, cycle_events)
    if send_feishu_webhook_text(text):
        logging.info("已推送本轮决策摘要到飞书。")
