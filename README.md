# A股量化模拟交易系统 (基于 MCP 与 AI Agent)

这是一个完全基于 Model Context Protocol (MCP) 与大语言模型（AI Agent）驱动的A股全自动量化模拟交易系统。系统通过双路 MCP 连接，实现“数据获取 - 知识检索 - 逻辑预判 - 模拟交易 - 资金管理”的全链路自动化闭环。

## 🌟 核心特性

- **双路 MCP 驱动引擎**
  - **`stock-sdk-mcp`**: 实时获取 A 股行情数据（包括涨幅榜、资金流向等作为决策上下文），以及持仓股票的最新价。
  - **`notebooklm-mcp`**: 挂载专属的 NotebookLM 知识库（例如包含《Factions and Finance in China》宏观分析），赋予智能体多域预判能力。
- **严格的交易纪律管理**
  - 单只股票持仓周期 15 天，目标收益率设定为 15%。
  - T+0 锁仓限制：买入后强制锁仓至少 1 小时方可卖出。
  - 动态止损/止盈：自动计算并执行回撤止损（默认 -5%）与止盈（默认 +15%）策略。
- **全息资金与持仓追踪**
  - 支持自定义初始资金（默认 100 万元），资金余额与总资产实时更新。
  - 严格计算交易成本（包含万三佣金与万五印花税）。
- **结构化决策日志**
  - 使用 SQLite 数据库持久化存储：系统不仅记录交易流水，还会完整记录每次智能体的 **Prompt、知识库引用片段以及原始决策逻辑**，方便事后回溯与归因分析。

## 📁 项目结构

```text
quant_sim/
├── config.yaml          # 核心配置文件（资金、费率、策略参数、MCP配置）
├── main.py              # 主调度程序（5分钟定时执行决策循环）
├── mcp_agent.py         # MCP 客户端封装（对接 Stock 与 NotebookLM）
├── portfolio.py         # 资产与订单管理器（买卖逻辑、费用计算、退出条件判断）
├── database.py          # SQLite 数据库底层封装
├── test_system.py       # 单元测试（测试手续费、锁仓、止损等核心逻辑）
├── report.py            # 回测与运行报告生成器
└── requirements.txt     # Python 依赖清单
```

## 🚀 快速开始

### 1. 环境准备

确保您的机器已安装 Python 3.10+ 和 Node.js（用于运行 MCP）。
同时，您需要在 Trae 或本地环境中配置好并登录 NotebookLM。

### 2. 安装依赖

```bash
# 建议使用虚拟环境
python -m venv .venv
source .venv/Scripts/activate  # Windows
# source .venv/bin/activate    # macOS/Linux

pip install -r requirements.txt
```

### 3. 修改配置

打开 `config.yaml`，根据您的实际环境修改以下字段：
- `initial_capital`: 初始模拟资金。
- `notebook_id`: 您在 NotebookLM 中对应的知识库 ID。
- `system_prompt`: 智能体的核心人设与选股要求。

### 4. 运行系统

```bash
# 启动 5 分钟定时自动交易循环
python main.py
```

### 5. 运行测试与生成报告

```bash
# 执行核心逻辑单元测试
python test_system.py

# 生成当前账户状态与交易记录报告（将输出到 reports 目录）
python report.py
```

## 🧠 决策工作流

1. **定时触发**：`main.py` 每 5 分钟唤醒一次 `MCPAgent`。
2. **状态更新**：系统调用 `stock-sdk-mcp` 更新所有持仓的当前价格，并由 `portfolio.py` 检查是否触发止盈、止损或最大持仓天数。如果触发，立即卖出并扣除印花税/佣金。
3. **寻找机会**：若账户有可用资金，拉取当前大盘/关注池的行情摘要。
4. **Agent 预判**：将行情摘要发给 `notebooklm-mcp`，智能体基于设定的知识库和人设，输出结构化的 JSON 选股决策（含目标价、止损价、买入理由）。
5. **执行买入**：系统验证资金与信号有效性后，以 100 股为单位执行买入操作，并记录决策逻辑至 SQLite。

## 📝 License

MIT License
