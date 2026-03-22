# A股量化模拟交易系统 (MCP + NotebookLM + 本地RAG)

这是一个面向 A 股模拟交易的自动化系统。  
核心思路是：用 MCP 获取实时行情，用 NotebookLM + 本地 RAG 提供知识证据，由 Agent 输出结构化决策，再由交易与风控模块执行。

---

## 1. 当前项目能做什么

- **定时轮询（live）**：按固定分钟间隔自动跑一轮决策。
- **持仓管理**：支持 T+1、买入后锁仓、止损、移动止盈、部分减仓、持仓时长控制。
- **智能体买入决策**：仅在综合胜率达到阈值时买入。
- **智能体卖出复核**：每轮先让智能体评估 `hold / partial / sell`，再由规则兜底。
- **NotebookLM 为主，本地 RAG 为兜底**：
  - NotebookLM 可用时优先用 NotebookLM；
  - 不可用时回退到本地 RAG；
  - 本地 RAG 支持两阶段检索与多跳合并。
- **可追溯日志**：保存交易流水、MDA 快照、决策输入输出。

---

## 2. 目录与核心文件

```text
quant_sim/
├── config.yaml          # 核心配置（交易规则、MCP、Agent、RAG）
├── main.py              # live/backtest/chat 入口，定时调度在这里
├── mcp_agent.py         # MCP 调用、NotebookLM问答、多跳策略
├── portfolio.py         # 持仓与风控执行（sell/sell_partial/process_exits）
├── database.py          # SQLite封装
├── report.py            # Markdown/HTML 报告
├── web_app.py           # Web 聊天和看板
└── requirements.txt
```

文件治理约定：
- `quant_sim/data/`：受版本管理的输入数据（如回测行情）
- `quant_sim/knowledge_base/raw/`：本地知识原始文档
- `quant_sim/knowledge_base/processed/`、`quant_sim/knowledge_base/index/`：索引产物
- `quant_sim/logs/`、`quant_sim/reports/`、`quant_sim/*.db`：运行产物

---

## 3. 环境准备

前置：
- Python 3.10+
- Node.js（用于 MCP 命令）
- 已可用的 NotebookLM 账户与 `notebooklm-mcp`

安装 MCP（示例）：

```bash
npm install -g stock-sdk-mcp
```

安装 Python 依赖：

```bash
cd quant_sim
python -m venv .venv
.\.venv\Scripts\activate   # Windows
pip install -r requirements.txt
```

---

## 4. 启动、关闭与常用命令

### 4.1 live 轮询（自动交易循环）

```bash
cd quant_sim
python -u main.py --mode live --interval 5
```

- `--interval 5` 表示每 5 分钟一轮
- 前台运行时，按 `Ctrl + C` 停止

后台运行（Windows PowerShell）：

```powershell
Start-Process python -ArgumentList "main.py --mode live --interval 5" -WorkingDirectory "E:\duovi\quant_sim"
```

后台停止（Windows PowerShell）：

```powershell
Get-CimInstance Win32_Process |
  Where-Object { $_.CommandLine -match "quant_sim\\main.py|quant_sim/main.py" } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
```

### 4.2 chat 模式（持续问答）

```bash
python -u main.py --mode chat
```

### 4.3 backtest 模式（历史回测）

```bash
python -u main.py --mode backtest
```

---

## 5. 系统运行逻辑（重点）

每轮 `run_decision_cycle()` 的主流程：

1. **交易时段检查**
   - 非交易时段默认观望（除非手动 `force=True`）。

2. **更新持仓价格（MCP 行情）**
   - 对当前持仓调用行情 MCP 获取最新价并写回数据库。

3. **智能体卖出复核（新增）**
   - 使用 NotebookLM + 行情上下文，对每个持仓输出 `hold / partial / sell`。
   - 仅当置信度达到 `agent.exit_review.min_confidence` 且满足 `can_sell`（T+1 + 锁仓）才执行。

4. **规则兜底卖出**
   - 再执行 `portfolio.process_exits()`：
   - 包含硬止损、移动止盈、强制减仓、时间止损等规则。

5. **买入决策**
   - 当仓位与现金条件满足时，采集市场数据，让智能体输出买入决策。
   - 仅当综合胜率 `>= trading.win_rate_threshold` 才买入。

6. **记录快照与报告**
   - 写入决策日志、MDA 快照，刷新 `reports/dashboard.html`。

---

## 6. 检索与推理逻辑（NotebookLM + 本地 RAG）

### 6.1 主路径

- 默认先走 NotebookLM（知识库主证据源）。
- `multi-domain-foresight` 可在首轮证据不足时发起 NotebookLM 第二跳补全（gap fill）。

### 6.2 本地 RAG 兜底

当 NotebookLM 不可用时，回退到本地 RAG：

- **two_stage**：粗召回 + 融合精排
- **multihop**：多查询后缀检索 + 去重合并

---

## 7. 配置说明（`config.yaml`）

### 7.1 交易参数（`trading`）

- `initial_capital`：初始资金
- `stop_loss`：基础止损比例（如 `-0.05`）
- `partial_take_at_return`：达到该收益率触发强制减仓
- `partial_take_ratio`：减仓比例
- `win_rate_threshold`：买入阈值
- `sell_lock_minutes`：买入后锁仓分钟数

### 7.2 MCP（`mcp`）

- `stock_server` / `ashare_server`：行情 MCP
- `notebooklm_server` + `notebook_id`：NotebookLM MCP
- `market_tool_candidates`：行情工具候选映射

### 7.3 本地 RAG（`local_rag`）

- `enabled`：是否启用本地 RAG
- `two_stage.*`：两阶段检索参数
- `multihop.*`：多跳检索参数

### 7.4 Agent（`agent`）

- `orchestrator_prompt` / `execution_prompt`：双提示词
- `retrieval_protocol_prompt`：检索协议约束
- `multihop.*`：NotebookLM 第二跳缺口补全阈值
- `exit_review.enabled`：是否启用“每轮智能体卖出复核”
- `exit_review.min_confidence`：卖出建议执行置信度下限

---

## 8. Web 模式（`web_app.py`）

```bash
python web_app.py
```

- 默认地址：`http://127.0.0.1:7860`
- 支持连续问答、`Check/刷新` 手动触发一轮决策、看板展示
- 注意：**Web 进程本身不是 live 轮询服务**，自动轮询仍以 `main.py --mode live` 为主

---

## 9. 报告与测试

生成报告：

```bash
python report.py
```

运行测试：

```bash
python test_system.py
```

---

## License

MIT License
