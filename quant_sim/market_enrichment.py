"""Tushare / AKShare / Tavily 行情与新闻增强（fail-open，失败打 WARNING）。

A 股日线：默认 Tushare 优先，`enrichment.akshare_enabled` 开启时对未命中标的用
[AKShare](https://github.com/akfamily/akshare) 补缺（无需 Tushare Token）。
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request
from datetime import date, timedelta
from typing import Any

TAVILY_URL = "https://api.tavily.com/search"


def _log_api_failure(service: str, exc: BaseException | None = None, extra: str = ""):
    msg = f"[{service}] 调用失败"
    if extra:
        msg += f": {extra}"
    if exc is not None:
        msg += f" | {type(exc).__name__}: {exc}"
    logging.warning(msg)


def to_ts_code(sym: str) -> str | None:
    s = re.sub(r"\s+", "", sym or "")
    if not s:
        return None
    low = s.lower()
    m = re.match(r"^(?:sh|sz)?(\d{6})$", low)
    if m:
        code = m.group(1)
    else:
        m2 = re.search(r"(\d{6})", s)
        if not m2:
            return None
        code = m2.group(1)
    if low.startswith("sh") or code.startswith(("5", "6", "9")):
        return f"{code}.SH"
    if low.startswith("sz") or code.startswith(("0", "1", "2", "3")):
        return f"{code}.SZ"
    if code.startswith("6"):
        return f"{code}.SH"
    return f"{code}.SZ"


def to_6_digit_a_code(sym: str) -> str | None:
    """AKShare `stock_zh_a_hist` 使用 6 位数字代码。"""
    ts = to_ts_code(sym)
    if not ts:
        return None
    return ts.split(".")[0]


def _tushare_pro():
    token = (os.environ.get("TUSHARE_TOKEN") or "").strip()
    if not token:
        return None
    try:
        import tushare as ts

        return ts.pro_api(token)
    except Exception as e:
        _log_api_failure("Tushare", e, "无法初始化 pro_api")
        return None


def _date_window_14d() -> tuple[str, str]:
    end = date.today()
    start = end - timedelta(days=14)
    return start.strftime("%Y%m%d"), end.strftime("%Y%m%d")


def _tushare_fill_prices_impl(symbols: list[str], cfg: dict[str, Any]) -> dict[str, float]:
    pro = _tushare_pro()
    if not pro or not symbols:
        return {}
    start_s, end_s = _date_window_14d()
    out: dict[str, float] = {}
    for raw in symbols:
        sym = str(raw).strip()
        ts_code = to_ts_code(sym)
        if not ts_code:
            continue
        try:
            df = pro.daily(ts_code=ts_code, start_date=start_s, end_date=end_s)
            if df is None or df.empty:
                logging.warning("[Tushare] %s 无日线返回（可能非交易日或权限不足）", ts_code)
                continue
            df = df.sort_values("trade_date", ascending=False)
            close = float(df.iloc[0]["close"])
            out[sym] = close
        except Exception as e:
            _log_api_failure("Tushare", e, f"ts_code={ts_code}")
    return out


def _akshare_daily_df(sym: str, cfg: dict[str, Any]):
    code = to_6_digit_a_code(sym)
    if not code:
        return None
    try:
        import akshare as ak
    except ImportError as e:
        _log_api_failure("AKShare", e, "未安装 akshare，请 pip install akshare")
        return None
    start_s, end_s = _date_window_14d()
    try:
        df = ak.stock_zh_a_hist(
            symbol=code,
            period="daily",
            start_date=start_s,
            end_date=end_s,
            adjust="",
        )
        return df
    except Exception as e:
        _log_api_failure("AKShare", e, f"symbol={code}")
        return None


def _akshare_fill_prices_impl(symbols: list[str], cfg: dict[str, Any]) -> dict[str, float]:
    if not symbols:
        return {}
    out: dict[str, float] = {}
    for raw in symbols:
        sym = str(raw).strip()
        ts_code = to_ts_code(sym)
        if not ts_code:
            continue
        df = _akshare_daily_df(sym, cfg)
        if df is None or df.empty:
            logging.warning("[AKShare] %s 无日线返回（可能非交易日或源站异常）", ts_code)
            continue
        date_col = "日期" if "日期" in df.columns else None
        close_col = "收盘" if "收盘" in df.columns else None
        if not date_col or not close_col:
            _log_api_failure("AKShare", None, f"{ts_code} 返回列不符合预期: {list(df.columns)}")
            continue
        try:
            sdf = df.sort_values(date_col, ascending=False)
            close = float(sdf.iloc[0][close_col])
            out[sym] = close
        except Exception as e:
            _log_api_failure("AKShare", e, f"parse {ts_code}")
    return out


def tushare_fill_prices(symbols: list[str], cfg: dict[str, Any] | None = None) -> dict[str, float]:
    """先 Tushare（若启用），对仍未命中代码再用 AKShare（若启用）。"""
    cfg = cfg or {}
    if not symbols:
        return {}
    out: dict[str, float] = {}
    if cfg.get("tushare_enabled", True):
        out.update(_tushare_fill_prices_impl(symbols, cfg))
    if cfg.get("akshare_enabled", True):
        missing = [s for s in symbols if str(s).strip() not in out]
        if missing:
            out.update(_akshare_fill_prices_impl(missing, cfg))
    return out


def _tushare_summary_lines_impl(symbols: list[str], cfg: dict[str, Any]) -> tuple[list[str], set[str]]:
    pro = _tushare_pro()
    lines: list[str] = []
    covered: set[str] = set()
    if not pro or not symbols:
        return lines, covered
    start_s, end_s = _date_window_14d()
    for raw in symbols[:20]:
        sym = str(raw).strip()
        ts_code = to_ts_code(sym)
        if not ts_code:
            continue
        try:
            df = pro.daily(ts_code=ts_code, start_date=start_s, end_date=end_s)
            if df is None or df.empty:
                continue
            df = df.sort_values("trade_date", ascending=False)
            row = df.iloc[0]
            lines.append(
                f"[Tushare] {sym}({ts_code}) 最近交易日 {row.get('trade_date')} "
                f"收 {row['close']} 涨跌额 {row.get('change', '')} 换手 {row.get('pct_chg', '')}%"
            )
            covered.add(sym)
        except Exception as e:
            _log_api_failure("Tushare", e, f"summary ts_code={ts_code}")
    return lines, covered


def _akshare_summary_line(sym: str, cfg: dict[str, Any]) -> str | None:
    ts_code = to_ts_code(sym)
    if not ts_code:
        return None
    df = _akshare_daily_df(sym, cfg)
    if df is None or df.empty:
        return None
    date_col = "日期" if "日期" in df.columns else None
    close_col = "收盘" if "收盘" in df.columns else None
    if not date_col or not close_col:
        return None
    try:
        sdf = df.sort_values(date_col, ascending=False)
        row = sdf.iloc[0]
        chg = row.get("涨跌幅", row.get("涨跌额", ""))
        tor = row.get("换手率", "")
        return (
            f"[AKShare] {sym}({ts_code}) 最近交易日 {row.get(date_col)} "
            f"收 {row[close_col]} 涨跌幅 {chg} 换手 {tor}"
        )
    except Exception as e:
        _log_api_failure("AKShare", e, f"summary {ts_code}")
        return None


def tushare_summary_lines(symbols: list[str], cfg: dict[str, Any] | None = None) -> str:
    """先 Tushare 摘要，未覆盖的标的再用 AKShare。"""
    cfg = cfg or {}
    syms = [str(s).strip() for s in (symbols or [])[:20] if str(s).strip()]
    if not syms:
        return ""
    lines: list[str] = []
    covered: set[str] = set()
    if cfg.get("tushare_enabled", True):
        tl, tc = _tushare_summary_lines_impl(syms, cfg)
        lines.extend(tl)
        covered |= tc
    if cfg.get("akshare_enabled", True):
        for sym in syms:
            if sym in covered:
                continue
            al = _akshare_summary_line(sym, cfg)
            if al:
                lines.append(al)
                covered.add(sym)
    if not lines:
        return ""
    return "【A股日线摘要】\n" + "\n".join(lines)


def _tavily_api_keys() -> list[str]:
    raw = (os.environ.get("TAVILY_API_KEYS") or os.environ.get("TAVILY_API_KEY") or "").strip()
    if not raw:
        return []
    return [k.strip() for k in raw.split(",") if k.strip()]


def tavily_search(query: str, api_key: str, max_results: int = 3, timeout: float = 12.0) -> list[dict[str, Any]]:
    payload = json.dumps(
        {
            "api_key": api_key,
            "query": query,
            "search_depth": "basic",
            "max_results": max_results,
            "include_answer": False,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    req = urllib.request.Request(
        TAVILY_URL,
        data=payload,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")[:500]
        _log_api_failure("Tavily", e, f"HTTP {e.code} {err_body}")
        return []
    except Exception as e:
        _log_api_failure("Tavily", e)
        return []
    results = body.get("results") or []
    if not isinstance(results, list):
        return []
    return results


def tavily_news_block(queries: list[str], cfg: dict[str, Any] | None = None) -> str:
    cfg = cfg or {}
    keys = _tavily_api_keys()
    if not keys or not queries:
        return ""
    max_q = int(cfg.get("news_max_queries", 4))
    max_results = int(cfg.get("tavily_max_results", 3))
    timeout = float(cfg.get("timeout_seconds", 12))
    lines: list[str] = []
    key_idx = 0
    for q in queries[:max_q]:
        q = (q or "").strip()
        if not q:
            continue
        api_key = keys[key_idx % len(keys)]
        key_idx += 1
        hits = tavily_search(q, api_key, max_results=max_results, timeout=timeout)
        if not hits:
            continue
        lines.append(f"查询「{q}」:")
        for h in hits:
            title = (h.get("title") or "")[:80]
            content = (h.get("content") or "")[:200]
            url = h.get("url") or ""
            lines.append(f"  - {title} {content} ({url})")
    if not lines:
        return ""
    return "【Tavily 新闻摘要】\n" + "\n".join(lines)


def collect_stock_like_queries(symbol: str | None, name: str | None, index_queries: list[str]) -> list[str]:
    out: list[str] = []
    for item in index_queries or []:
        s = str(item).strip()
        if not s:
            continue
        if re.search(r"\d{6}", s):
            out.append(f"A股 {s} 最新行情 消息")
        else:
            out.append(f"{s} A股市场 新闻")
    if symbol:
        out.append(f"A股股票 {symbol} {name or ''} 最新消息 研报")
    return out[:12]
