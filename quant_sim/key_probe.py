"""启动时轻量探针：Tushare / Tavily / LiteLLM 是否可用。"""

from __future__ import annotations

import asyncio
import logging

from llm_decision import resolve_litellm_model_chain
from market_enrichment import _tushare_pro, _tavily_api_keys, tavily_search, to_6_digit_a_code


async def run_key_probe(config: dict | None = None) -> None:
    config = config or {}
    lines: list[str] = ["=== quant_sim API 探针（低开销）==="]

    pro = _tushare_pro()
    if not pro:
        lines.append("Tushare: 跳过（未配置 TUSHARE_TOKEN 或初始化失败）")
    else:
        try:
            df = pro.trade_cal(exchange="SSE", start_date="20240101", end_date="20240107")
            if df is not None and len(df) > 0:
                lines.append("Tushare: OK（trade_cal 有返回）")
            else:
                lines.append("Tushare: 异常（trade_cal 无数据，可能权限/网络）")
        except Exception as e:
            lines.append(f"Tushare: 失败 {type(e).__name__}: {str(e)[:200]}")

    enrich = (config.get("enrichment") or {}) if isinstance(config, dict) else {}
    if not enrich.get("akshare_enabled", True):
        lines.append("AKShare: 跳过（config enrichment.akshare_enabled=false）")
    else:
        try:
            import akshare as ak
        except ImportError as e:
            lines.append(f"AKShare: 未安装（pip install akshare） {e}")
        else:
            code = to_6_digit_a_code("sh600519") or "600519"
            try:
                df = ak.stock_zh_a_hist(
                    symbol=code,
                    period="daily",
                    start_date="20240101",
                    end_date="20240110",
                    adjust="",
                )
                if df is not None and len(df) > 0:
                    lines.append(f"AKShare: OK（stock_zh_a_hist {code} 有返回）")
                else:
                    lines.append("AKShare: 异常（无数据，可能源站/网络）")
            except Exception as e:
                lines.append(f"AKShare: 失败 {type(e).__name__}: {str(e)[:200]}")

    keys = _tavily_api_keys()
    if not keys:
        lines.append("Tavily: 跳过（未配置 TAVILY_API_KEYS）")
    else:
        hits = tavily_search("上证指数 收盘", keys[0], max_results=1, timeout=10.0)
        if hits:
            lines.append("Tavily: OK")
        else:
            lines.append("Tavily: 失败或无结果（检查 Key 额度/网络）")

    litellm_cfg = (config.get("litellm") or {}) if isinstance(config, dict) else {}
    models = resolve_litellm_model_chain(litellm_cfg)
    if not models:
        lines.append("LiteLLM: 跳过（未配置 LITELLM_MODEL / primary_model）")
    else:
        timeout = float(litellm_cfg.get("timeout_seconds", 30))

        def _ping():
            import litellm

            return litellm.completion(
                model=models[0],
                messages=[{"role": "user", "content": "Reply with exactly: OK"}],
                max_tokens=8,
                timeout=timeout,
            )

        try:
            r = await asyncio.to_thread(_ping)
            content = (r.choices[0].message.content or "").strip()
            if content:
                lines.append(f"LiteLLM: OK（模型 {models[0]}，响应片段: {content[:40]}）")
            else:
                lines.append(f"LiteLLM: 异常（模型 {models[0]} 空响应）")
        except Exception as e:
            lines.append(
                f"LiteLLM: 失败 {type(e).__name__}: {str(e)[:220]}（检查 Key/额度/模型名）"
            )

    for line in lines:
        logging.warning(line)
    logging.warning("=== 探针结束 ===")
