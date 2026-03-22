"""LiteLLM 结构化决策调用（与 NotebookLM 输出 JSON 字段对齐）。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Any, Callable

ExtractJsonFn = Callable[[str | None], str | None]


def resolve_litellm_model_chain(cfg: dict[str, Any] | None = None) -> list[str]:
    cfg = cfg or {}
    chain: list[str] = []
    primary = (cfg.get("primary_model") or os.getenv("LITELLM_MODEL") or "").strip()
    if primary:
        chain.append(primary)
    fallbacks = cfg.get("fallback_models")
    if isinstance(fallbacks, str):
        fallbacks = [x.strip() for x in fallbacks.split(",") if x.strip()]
    elif not isinstance(fallbacks, list):
        fallbacks = []
    env_fb = os.getenv("LITELLM_FALLBACK_MODELS") or ""
    merged: list[str] = list(fallbacks)
    for part in env_fb.split(","):
        p = part.strip()
        if p and p not in merged:
            merged.append(p)
    for m in merged:
        if m and m not in chain:
            chain.append(m)
    return chain


def phase1_screening_json_instruction() -> str:
    return """
只输出一个 JSON 对象（不要 markdown 代码块），字段如下：
{
  "market_narrative": "基于当前盘面摘要与知识库，用中文简要梳理大盘/板块环境与选股思路；说明池内/池外标的（若有池）。仅供研究模拟，不构成投资建议。",
  "candidates": [
    {
      "symbol": "6位A股代码",
      "name": "简称，可空",
      "thesis": "入选的一句话逻辑",
      "from_pool": true
    }
  ]
}
candidates 数量须满足用户消息中的 min/max 与股票池硬规则；from_pool 与规则一致。
""".strip()


def decision_json_instruction() -> str:
    return """
你必须只输出一个 JSON 对象（不要 markdown 代码块），字段如下（数值用数字）：
{
  "symbol": "6位A股代码如 600519；无合适标的填 null",
  "name": "股票简称，可为空字符串",
  "win_rate_confidence": 0.0,
  "dimension_scores": {
    "data_arch": 0,
    "notebooklm": 0,
    "game_psych": 0,
    "trend": 0
  },
  "reason": "一句话结论",
  "target_price": null,
  "stop_loss_price": null,
  "position_pct": 0.3,
  "thinking_trace": {
    "data_arch": "",
    "notebooklm": "",
    "game_psych": "",
    "trend": ""
  },
  "intent_module": "MDA_execution",
  "deviation_analysis": "实时盘面/新闻相对知识库要点或纪律阈值的偏离，可简写；无则空字符串",
  "confidence_rationale": "用一两句话说明胜率估计依据（知识库+盘面），勿编造未给出的数据"
}
dimension_scores 四项各 0-25 分整数；win_rate_confidence 为 0-1 小数，可与四分之和/100 一致。
若无合格买点，symbol 置为 null，win_rate_confidence 取低分并说明原因。
""".strip()


def normalize_structured_decision(decision: dict[str, Any] | None) -> dict[str, Any]:
    """统一买入类决策 JSON：补默认字段、UTC 时间戳、执行审计占位（成交价由 main 在下单前补全）。"""
    from datetime import datetime, timezone

    d: dict[str, Any] = dict(decision) if isinstance(decision, dict) else {}
    d.setdefault("intent_module", "MDA_execution")
    d.setdefault("deviation_analysis", "")
    d.setdefault("confidence_rationale", "")
    d.setdefault("knowledge_evidence_bundle", {})
    d.setdefault("execution_audit", {})
    now_iso = datetime.now(timezone.utc).isoformat()
    d.setdefault("evidence_as_of", now_iso)
    if not (d.get("confidence_rationale") or "").strip():
        ts = d.get("total_score")
        wr = d.get("win_rate_confidence")
        ks = d.get("knowledge_source") or d.get("decision_source") or "unknown"
        d["confidence_rationale"] = (
            f"四维合计分约 {ts}，综合胜率估计 {wr}；决策来源 {d.get('decision_source', '')}；知识源 {ks}。"
        ).strip()
    ea = d["execution_audit"]
    if isinstance(ea, dict):
        ea.setdefault("evidence_as_of", d.get("evidence_as_of"))
        ea.setdefault("price_at_execution", None)
        ea.setdefault("executed_quantity", None)
        ea.setdefault("notional", None)
        if d.get("market_observability") is not None:
            ea.setdefault("market_context_at_decision", d.get("market_observability"))
    return d


def _sync_litellm_completion(model: str, user_content: str, timeout: float) -> Any:
    import litellm

    return litellm.completion(
        model=model,
        messages=[{"role": "user", "content": user_content}],
        timeout=timeout,
    )


async def run_litellm_decision_chain(
    user_prompt: str,
    models: list[str],
    timeout: float,
    extract_json: ExtractJsonFn,
) -> tuple[dict[str, Any] | None, str | None, str | None]:
    """返回 (decision_dict, raw_text, error_message)。"""
    if not models:
        return None, None, "未配置 LiteLLM 模型链"
    last_raw: str | None = None
    for model in models:
        try:
            resp = await asyncio.to_thread(_sync_litellm_completion, model, user_prompt, timeout)
            choice = (resp.choices or [None])[0]
            msg = getattr(choice, "message", None)
            content = (getattr(msg, "content", None) or "").strip() if msg else ""
            last_raw = content
            if not content:
                logging.warning("[LiteLLM] 模型 %s 返回空内容", model)
                continue
            json_str = extract_json(content)
            if not json_str:
                logging.warning("[LiteLLM] 模型 %s 返回内容无法解析 JSON", model)
                continue
            data = json.loads(json_str)
            if not isinstance(data, dict):
                continue
            return data, content, None
        except json.JSONDecodeError as e:
            logging.warning("[LiteLLM] 模型 %s JSON 解析失败: %s", model, e)
        except Exception as e:
            err_s = str(e)
            logging.warning(
                "[LiteLLM] 模型 %s 调用失败: %s | %s",
                model,
                type(e).__name__,
                err_s[:400],
            )
            if re.search(r"402|401|403|429|insufficient|quota|billing|invalid.*key", err_s, re.I):
                logging.warning(
                    "[LiteLLM] 模型 %s 可能欠费/Key 无效/限流，将尝试下一模型",
                    model,
                )
    return None, last_raw, "LiteLLM 全部模型均未返回有效 JSON"
