# Quant Sim File Layout

This project keeps source code, tracked inputs, and generated runtime artifacts separate.

## Source Of Truth

- `quant_sim/config.yaml`: trading, MCP, and agent configuration.
- `quant_sim/main.py`: system entrypoint and decision-cycle orchestration.
- `quant_sim/feishu_notify.py`: optional Feishu custom-bot webhook (`FEISHU_WEBHOOK_URL`) for cycle summaries.
- `quant_sim/market_enrichment.py`: Tushare + AKShare (fallback) + Tavily enrichment for `get_market_data` / `update_holdings_prices`.
- `quant_sim/llm_decision.py`: LiteLLM JSON decision chain helpers.
- `quant_sim/key_probe.py`: `--probe-keys` connectivity checks.
- `quant_sim/screening_universe.py`: two-stage screening stock pool load + candidate validation.
- `quant_sim/docs/ENV_AND_DATA_SOURCES.md`: env keys, decision backends, and data-source fallbacks.
- `quant_sim/mcp_agent.py`: MCP market tools, NotebookLM, local RAG (`local_rag.py`), and structured buy/exit decisions.
- `quant_sim/risk_gate.py`: position cap, cash-ratio scan, optional block when KB unavailable, and optional `require_notebooklm_for_buy` (only `decision_source`/`knowledge_source` == `notebooklm` may open positions).
- `quant_sim/portfolio.py`: position, exit, and risk-control rules.
- `quant_sim/database.py`: SQLite schema and persistence helpers.
- `quant_sim/report.py`: dashboard and report generation.
- `quant_sim/web_app.py`: Flask UI and API routes.
- `quant_sim/web_bridge.py`: subprocess bridge for expensive or isolated operations.
- `quant_sim/test_system.py`: regression tests for trading rules.

## Tracked Input Data

- `quant_sim/data/`: curated datasets that are intended to be versioned, such as `historical_quotes.csv`.
- `quant_sim/docs/`: project-facing documentation and operating conventions.

## Generated Runtime Artifacts

Do not treat these as source files or hand-edit them unless debugging something specific.

- `quant_sim/logs/`: runtime logs.
- `quant_sim/reports/`: generated dashboards, markdown reports, and trade exports.
- `quant_sim/quant_sim.db`: main simulation database.
- `quant_sim/test_quant_sim*.db`: temporary test databases.
- `quant_sim/__pycache__/`: Python bytecode cache.

## Knowledge Base Layout

Reserve these paths for local knowledge workflows:

- `quant_sim/knowledge_base/raw/`: manually collected research material, markdown notes, and extracted PDF text.
- `quant_sim/knowledge_base/processed/`: normalized chunks derived from raw material.
- `quant_sim/knowledge_base/index/`: local vector index or retrieval state.

Only `raw/` should normally be reviewed as human-authored content. `processed/` and `index/` are generated artifacts.

## Decision pipeline (MCP → knowledge → agent → portfolio)

End-to-end flow for **live** mode (`runtime.mode: live` in `config.yaml`):

1. **MCP market data**: `MCPAgent.get_market_data` and `update_holdings_prices` call the configured stock MCP (`mcp.stock_server`, optional `mcp.ashare_server`) using tool names under `mcp.market_tool_candidates` (quotes, intraday, fundamentals, etc.). Tushare/Tavily enrichment is controlled by `enrichment.*`.
2. **Knowledge**: Primary path is NotebookLM via `mcp.notebooklm_server` and `mcp.notebook_id`. Local evidence uses `LocalKnowledgeBase` (`local_rag` in `config.yaml`). With `agent.dual_l1_evidence.enabled: true`, each successful LiteLLM/NotebookLM buy decision also attaches a compact local-RAG bundle for cross-checking.
3. **Agent**: `runtime.decision_backend` selects LiteLLM (`llm_decision.py`), NotebookLM, and/or local RAG fallbacks. Structured fields are normalized by `llm_decision.normalize_structured_decision` (timestamps, audit placeholders, optional `deviation_analysis` / `confidence_rationale`).
4. **Risk + execution**: `main.py` `QuantTradingSystem.run_decision_cycle` applies `risk_gate` (`risk_gate` block in `config.yaml`) then `PortfolioManager` rules (`trading.*` in `config.yaml`: stop-loss, locks, win-rate threshold, etc.).

Orchestration entrypoint: `main.py` (scheduled or manual refresh). Persistence: `database.py` (`decision_logs`, `mda_snapshots` store the full decision JSON in `logic`).

## Working Rules

- Add new runtime outputs under an existing generated directory before creating new top-level paths.
- Prefer extending an existing module over adding one-off scripts in `quant_sim/`.
- If a script is purely operational or diagnostic, place it under a dedicated subdirectory instead of the project root.
- When adding a new directory, decide up front whether it is source, tracked input, or generated output, and update `.gitignore` and this document together.
