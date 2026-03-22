"""两阶段选股：股票池加载与候选校验。"""

from __future__ import annotations

import os
import re
from typing import Any


def extract_symbol_6(s: str | None) -> str | None:
    if not s:
        return None
    m = re.search(r"(\d{6})", str(s))
    return m.group(1) if m else None


def load_universe(cfg_screen: dict[str, Any] | None) -> list[str]:
    cfg_screen = cfg_screen or {}
    keys = cfg_screen.get("universe_env_keys") or ["STOCK_UNIVERSE", "STOCK_LIST"]
    seen: set[str] = set()
    out: list[str] = []
    for key in keys:
        raw = (os.environ.get(key) or "").strip()
        for part in raw.split(","):
            sym = extract_symbol_6(part)
            if sym and sym not in seen:
                seen.add(sym)
                out.append(sym)
    for part in cfg_screen.get("universe_symbols") or []:
        sym = extract_symbol_6(str(part))
        if sym and sym not in seen:
            seen.add(sym)
            out.append(sym)
    return out


def build_rules_text(universe: list[str], min_n: int, max_n: int) -> str:
    if not universe:
        return (
            f"当前未配置股票池。请基于盘面与知识库，自由提名 {min_n}～{max_n} 只 A 股候选（6 位代码），"
            f"并写清每只的简短逻辑。输出 JSON 中须标注 from_pool 均为 false。"
        )
    if len(universe) >= min_n:
        return (
            f"已配置股票池（共 {len(universe)} 只）：{', '.join(universe)}。\n"
            f"硬规则：候选必须全部来自该池，数量 {min_n}～{max_n} 只，不得引入池外代码。\n"
            f"每只标注 from_pool=true。"
        )
    return (
        f"已配置股票池（共 {len(universe)} 只，少于 {min_n}）：{', '.join(universe)}。\n"
        f"硬规则：上述池内每一只都必须出现在 candidates 中；其余名额可从全市场自由提名补足，"
        f"使候选总数在 {min_n}～{max_n} 之间。池内标的 from_pool=true，补足标的 from_pool=false。\n"
        f"在 market_narrative 中简要说明哪些来自池内、哪些为补足提名。本输出仅供研究模拟，不构成投资建议。"
    )


def validate_candidates(
    candidates: list[dict[str, Any]] | None,
    universe: list[str],
    min_n: int,
    max_n: int,
) -> tuple[list[dict[str, Any]] | None, str | None]:
    """返回 (修正后的候选列表, 错误信息)。"""
    order: list[str] = []
    by_sym: dict[str, dict[str, Any]] = {}
    for c in candidates or []:
        if not isinstance(c, dict):
            continue
        sym = extract_symbol_6(str(c.get("symbol") or c.get("code") or ""))
        if not sym:
            continue
        if sym not in by_sym:
            order.append(sym)
        by_sym[sym] = {
            "symbol": sym,
            "name": str(c.get("name") or "").strip(),
            "thesis": str(c.get("thesis") or c.get("reason") or "").strip(),
            "from_pool": bool(c.get("from_pool")),
        }

    u_norm: list[str] = []
    seen_u: set[str] = set()
    for x in universe:
        sx = extract_symbol_6(str(x))
        if sx and sx not in seen_u:
            seen_u.add(sx)
            u_norm.append(sx)
    u_set = set(u_norm)

    if not u_set:
        lst = [by_sym[s] for s in order][:max_n]
        if len(lst) < min_n:
            return None, f"无股票池时有效候选仅 {len(lst)} 只，少于 {min_n}"
        return lst, None

    if len(u_set) >= min_n:
        pool_only = [by_sym[s] for s in order if s in u_set]
        pool_only = pool_only[:max_n]
        if len(pool_only) < min_n:
            return None, f"池规模≥{min_n} 时，池内有效候选仅 {len(pool_only)} 只（需 {min_n}～{max_n}）"
        extras = [s for s in by_sym if s not in u_set]
        if extras:
            return None, f"池规模≥{min_n} 时不应出现池外代码: {extras}"
        return pool_only, None

    merged: list[dict[str, Any]] = []
    used: set[str] = set()
    for sym in u_norm:
        if sym in by_sym:
            row = dict(by_sym[sym])
            row["from_pool"] = True
            merged.append(row)
        else:
            merged.append(
                {
                    "symbol": sym,
                    "name": sym,
                    "thesis": "(股票池必选，模型未返回该股，由系统占位)",
                    "from_pool": True,
                }
            )
        used.add(sym)
    for s in order:
        if s in used:
            continue
        if s in u_set:
            continue
        r = dict(by_sym[s])
        r["from_pool"] = False
        merged.append(r)
        used.add(s)
        if len(merged) >= max_n:
            break
    if len(merged) < min_n:
        for s in order:
            if s in used:
                continue
            r = dict(by_sym[s])
            r["from_pool"] = r.get("from_pool", False)
            merged.append(r)
            used.add(s)
            if len(merged) >= min_n:
                break
    if len(merged) < min_n:
        return None, f"小股票池补足后仅 {len(merged)} 只，需至少 {min_n}"
    return merged[:max_n], None
