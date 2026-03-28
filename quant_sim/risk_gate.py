import yaml
from typing import Any
import logging

class RiskGate:
    def __init__(self, config: dict[str, Any] | None = None):
        cfg = config or {}
        self._rg = cfg.get("risk_gate") or {}
        self._trading = cfg.get("trading") or {}
        self._llm_decision_flow = cfg.get("llm_decision_flow") or {}

    def max_positions(self) -> int:
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
        return bool(self._rg.get("require_notebooklm_for_buy", False))

    def _get_effective_decision_source(self, decision: dict[str, Any]) -> str:
        """根据 LLM 决策流配置，确定有效的决策来源"""
        primary_llm = self._llm_decision_flow.get("primary_llm")
        fallback_llm = self._llm_decision_flow.get("fallback_llm")
        pure_quant_fallback_enabled = self._llm_decision_flow.get("pure_quant_fallback_enabled", False)

        decision_source = str(decision.get("decision_source") or decision.get("knowledge_source") or "").strip().lower()
        decision_success = decision.get("success", False)

        if decision_success:
            if primary_llm and primary_llm in decision_source:
                return primary_llm
            if fallback_llm and fallback_llm in decision_source:
                return fallback_llm
        
        if pure_quant_fallback_enabled and decision_source == "pure_quant":
            return "pure_quant"

        return "unknown"

    def buy_blocked_reason(self, decision: dict[str, Any] | None, positions_count: int) -> str | None:
        """若不应开仓则返回人类可读原因，否则 None。"""
        if positions_count >= self.max_positions():
            return f"持仓已达上限（{self.max_positions()} 只），本轮不新增买入。"
        
        d = decision or {}

        if self.require_notebooklm_for_buy():
            effective_source = self._get_effective_decision_source(d)
            if effective_source != "notebooklm":
                return (
                    f"require_notebooklm_for_buy：开仓必须走 NotebookLM 主路径，当前有效来源: {effective_source}"
                )
            if d.get("success") is False:
                return (
                    "require_notebooklm_for_buy：NotebookLM 路径未产出可执行决策（success=False），禁止开仓。"
                )

        # 如果 NotebookLM 不是强制要求，则根据 LLM 决策流进行判断
        if not self.require_notebooklm_for_buy():
            effective_source = self._get_effective_decision_source(d)
            if effective_source == "unknown" and not self._llm_decision_flow.get("pure_quant_fallback_enabled", False):
                return "未识别到有效决策来源，且纯量化回退未启用，禁止开仓。"
            if effective_source == "unknown" and self._llm_decision_flow.get("pure_quant_fallback_enabled", False) and d.get("decision_source") != "pure_quant":
                return "未识别到有效决策来源，且未回退到纯量化模式，禁止开仓。"

        if not self.block_buy_on_kb_unavailable():
            return None
            
        if d.get("success") is False:
            return "block_buy_on_kb_unavailable：决策 success=False，禁止开仓。"
            
        ks = str(d.get("knowledge_source") or "").strip().lower()
        if ks == "unavailable":
            return "block_buy_on_kb_unavailable：knowledge_source=unavailable，禁止开仓。"
            
        return None
