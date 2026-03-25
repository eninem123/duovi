"""可配置风控门禁：持仓上限、最低现金比例扫描、知识源与 NotebookLM 路径约束。"""

from typing import Any

class RiskGate:
    def __init__(self, config: dict[str, Any] | None = None):
        cfg = config or {}
        self._rg = cfg.get("risk_gate") or {}
        self._trading = cfg.get("trading") or {}

    def max_positions(self) -> int:
        # 优先从 risk_gate 读取，兼容 trading 配置
        try:
            val = self._rg.get("max_positions") or self._trading.get("max_positions") or 3
            n = int(val)
        except (TypeError, ValueError):
            n = 3
        return max(1, min(50, n))

    def min_cash_ratio_to_scan(self) -> float:
        try:
            r = float(self._rg.get("min_cash_ratio_to_scan", 0.2))
        except (TypeError, ValueError):
            r = 0.2
        return max(0.0, min(1.0, r))

    def block_buy_on_kb_unavailable(self) -> bool:
        return bool(self._rg.get("block_buy_on_kb_unavailable", False))

    def require_notebooklm_for_buy(self) -> bool:
        """为 true 时：仅当决策明确来自 NotebookLM 主路径（decision_source/knowledge_source）才允许开仓。"""
        return bool(self._rg.get("require_notebooklm_for_buy", False))

    def buy_blocked_reason(self, decision: dict[str, Any] | None, positions_count: int) -> str | None:
        """若不应开仓则返回人类可读原因，否则 None。"""
        if positions_count >= self.max_positions():
            return f"持仓已达上限（{self.max_positions()} 只），本轮不新增买入。"
        
        d = decision or {}

        if self.require_notebooklm_for_buy():
            src = str(d.get("decision_source") or d.get("knowledge_source") or "").strip().lower()
            if "notebooklm" not in src:
                return (
                    f"require_notebooklm_for_buy：开仓必须走 NotebookLM 主路径，当前来源: {src}"
                )
            if d.get("success") is False:
                return (
                    "require_notebooklm_for_buy：NotebookLM 路径未产出可执行决策（success=False），禁止开仓。"
                )

        if not self.block_buy_on_kb_unavailable():
            return None
            
        if d.get("success") is False:
            return "block_buy_on_kb_unavailable：决策 success=False，禁止开仓。"
            
        ks = str(d.get("knowledge_source") or "").strip().lower()
        if ks == "unavailable":
            return "block_buy_on_kb_unavailable：knowledge_source=unavailable，禁止开仓。"
            
        return None
