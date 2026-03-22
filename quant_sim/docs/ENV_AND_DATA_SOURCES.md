# 环境变量与数据源说明

与 `daily_stock_analysis`（DSA）对齐的变量名可直接写在 `quant_sim/.env`，或通过环境变量 `QUANT_SIM_ENV_FILE` 指向另一份 `.env`（例如 DSA 根目录下的文件）。

## 决策后端 `runtime.decision_backend`（`config.yaml`）

| 值 | 行为 |
|----|------|
| `hybrid`（默认） | 先 LiteLLM（`LITELLM_MODEL` + 可选 fallback），失败再 NotebookLM；NotebookLM 失败仍走原有本地 RAG 降级。 |
| `litellm` | 仅 LiteLLM，全失败则直接本地 RAG，不调用 NotebookLM。 |
| `notebooklm` | 与改造前一致，仅 NotebookLM + 本地 RAG。 |

## LiteLLM / 多模型

- `LITELLM_MODEL`：主模型，例如 `gemini/gemini-2.0-flash`、`openai/gpt-4o`（需对应 `GEMINI_API_KEY`、`OPENAI_API_KEY` 等，见 [LiteLLM 文档](https://docs.litellm.ai/docs/)）。
- `LITELLM_FALLBACK_MODELS`：逗号分隔的备用模型列表；也可在 `config.yaml` 的 `litellm.fallback_models` 中配置。
- `litellm.primary_model` 非空时覆盖环境中的主模型名。

常见错误日志中会包含 `401/402/429`、`quota`、`invalid` 等字样，多表示 Key 无效、欠费或限流，可依次尝试 fallback 模型。

## Tushare

- 官方安装与 Token 说明入口：[Tushare 文档 / 操作手册](https://tushare.pro/document/1)（`pip install tushare`、注册 Pro、个人中心获取 Token）。本仓库在 [requirements.txt](../requirements.txt) 中已约束 `tushare>=1.2.89`，在项目根执行 `pip install -r requirements.txt` 即可。
- 同页中的 **「Tushare Skills」**（如 OpenClaw `clawhub` / `npx skills`）面向对话式 AI 环境，**不是** `quant_sim` 运行所必需；本项目通过 [market_enrichment.py](../market_enrichment.py) 直接使用 `ts.pro_api(token)` 拉数，与官网示例里先 `set_token` 再调接口等价。
- `TUSHARE_TOKEN`：写入 `quant_sim/.env` 或环境变量后生效；必填方可启用日线拉取；**免费积分下多为日线，不等同于交易所实时 tick**。
- 权限不足或网络错误时日志为 `[Tushare] 调用失败`，持仓现价会尽量保留 MCP 一侧结果。若 Token 曾泄露，请在 Tushare 个人中心轮换。

## AKShare（与 Tushare 并列、失败补缺）

- 开源库：[AKShare](https://github.com/akfamily/akshare)（`pip install akshare`，已在 [requirements.txt](../requirements.txt) 中约束版本）。
- `config.yaml` 的 `enrichment.akshare_enabled`：为 `true` 时，**A 股日线补价与摘要**在 Tushare 未命中或不可用时自动用 `stock_zh_a_hist` 补缺；**无需 Token**，数据来自公开源，稳定性与合规请自行评估。
- 与 Tushare 同为日线级参考，**非交易所实时 tick**。

## Tavily

- `TAVILY_API_KEYS`：逗号分隔多 Key 轮询；亦支持单个 `TAVILY_API_KEY`。
- 搜索失败时仅省略新闻块，**不中断**主决策流程。

## 行情合并优先级

- `get_market_data`：在 MCP 结果后追加 A 股日线摘要（Tushare 优先、AKShare 补缺）与 Tavily 摘要（可在 `enrichment` 中分别关闭）。
- `update_holdings_prices`：默认先 MCP，再对**未命中**的代码用 Tushare 日线收盘价补全，仍缺则用 AKShare；`mcp_quotes_enabled: false` 时跳过 MCP，再走 Tushare/AKShare。

## 两阶段选股 `agent.two_stage_screening`（`config.yaml`）

- `enabled: true` 时，买入扫描先 **阶段一**（大盘 + 本地 RAG 摘录 + LiteLLM/NotebookLM 产出 `market_narrative` 与候选列表），再 **阶段二**（对候选批量拉 MCP 行情与前 N 只多维工具 + Tushare/AKShare 日线摘要），最后仍走 **`make_decision` 同一 JSON 契约** 与 `win_rate_threshold`。
- 阶段一失败或候选校验失败时，自动 **回退** 为单次 `get_market_data` + `make_decision`。
- **股票池**（按顺序读取环境变量，可在 `universe_env_keys` 中改）：默认 `STOCK_UNIVERSE`，再 `STOCK_LIST`（与 DSA 一致）；可与 `universe_symbols` YAML 列表 **并集去重**。
- **数量规则**：无池时候选 5～10 只自由提名；池规模 ≥5 时候选必须 **全为池内子集**；池规模 &lt;5 时 **池内标的必须出现**，不足由全市场补足至至少 5 只，总数 ≤10。

## 探针

```bash
python main.py --probe-keys
```

对 Tushare、`TAVILY` 首个 Key、首个 LiteLLM 模型各做一次低开销请求，结果以 `WARNING` 级别写入日志。
