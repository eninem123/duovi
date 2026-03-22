import sqlite3
import pandas as pd
import matplotlib.pyplot as plt
import os
import json
from datetime import datetime

class ReportGenerator:
    def __init__(self, db_path="quant_sim.db"):
        self.db_path = db_path

    def _safe_json_loads(self, text, default):
        if not text:
            return default
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return default

    def _format_lock_status(self, bought_at):
        if not bought_at:
            return "已解锁"
        bought_dt = datetime.fromisoformat(bought_at)
        remaining_seconds = (bought_dt.replace(microsecond=0) - datetime.now().replace(microsecond=0)).total_seconds()
        unlock_seconds = remaining_seconds + 30 * 60
        if unlock_seconds > 0:
            return f"锁定中({int((unlock_seconds + 59) // 60)}min)"
        return "已解锁"
        
    def generate_report(self, output_dir="reports"):
        if not os.path.exists(output_dir):
            os.makedirs(output_dir)
            
        conn = sqlite3.connect(self.db_path)
        
        # 1. 账户总体情况
        account_df = pd.read_sql("SELECT * FROM account", conn)
        
        # 2. 交易流水
        trades_df = pd.read_sql("SELECT * FROM trades", conn)
        
        # 计算胜率
        # 需要匹配买卖对，这里简单统计
        sells = trades_df[trades_df['action'] == 'SELL']
        
        report_text = f"""
# A股量化模拟交易系统 - 回测/运行报告

## 1. 账户摘要
* **初始资金**: {account_df['initial_capital'].iloc[0]:.2f} 元
* **当前总资产**: {account_df['total_assets'].iloc[0]:.2f} 元
* **当前可用余额**: {account_df['balance'].iloc[0]:.2f} 元
* **总盈亏**: {account_df['total_pnl'].iloc[0]:.2f} 元
* **累计收益率**: {(account_df['total_assets'].iloc[0] / account_df['initial_capital'].iloc[0] - 1) * 100:.2f}%

## 2. 交易统计
* **总交易笔数**: {len(trades_df)} 笔
* **卖出平仓笔数**: {len(sells)} 笔

## 3. 详细交易记录
"""
        
        # 写入 Markdown
        report_path = os.path.join(output_dir, "report.md")
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(report_text)
            
        # 导出 CSV
        trades_df.to_csv(os.path.join(output_dir, "trades_log.csv"), index=False, encoding='utf-8-sig')
        
        conn.close()
        print(f"✅ 报告已生成至 {output_dir} 目录")

    def generate_html_dashboard(self, output_dir="reports"):
        if not os.path.exists(output_dir):
            os.makedirs(output_dir)

        conn = sqlite3.connect(self.db_path)
        account_df = pd.read_sql("SELECT * FROM account", conn)
        trades_df = pd.read_sql("SELECT * FROM trades ORDER BY timestamp DESC", conn)
        positions_df = pd.read_sql("SELECT * FROM positions", conn)
        snapshot_df = pd.read_sql(
            "SELECT * FROM mda_snapshots ORDER BY timestamp DESC LIMIT 1", conn
        )
        conn.close()

        if account_df.empty:
            raise ValueError("账户数据为空，无法生成 HTML 仪表盘。")

        account = account_df.iloc[0]
        snapshot = snapshot_df.iloc[0].to_dict() if not snapshot_df.empty else {}
        returns = (account["total_assets"] / account["initial_capital"] - 1) * 100 if account["initial_capital"] else 0.0
        thinking_trace = self._safe_json_loads(snapshot.get("thinking_trace"), {})
        logic = self._safe_json_loads(snapshot.get("logic"), {})

        if not positions_df.empty:
            positions_df["unrealized_pct"] = ((positions_df["current_price"] / positions_df["avg_price"]) - 1.0) * 100
            positions_df["lock_status"] = positions_df["bought_at"].apply(self._format_lock_status)
            holding_label = " / ".join(
                f"{row['symbol']} {row['name']}" for _, row in positions_df.head(3).iterrows()
            )
            pnl_label = " / ".join(
                f"{row['symbol']} {row['unrealized_pct']:+.2f}%"
                for _, row in positions_df.head(3).iterrows()
            )
            qty_label = " / ".join(
                f"{int(row['quantity'])}股 / ¥{row['avg_price']:.2f}"
                for _, row in positions_df.head(3).iterrows()
            )
            lock_label = " / ".join(positions_df["lock_status"].head(3).tolist())
        else:
            holding_label = "空仓"
            pnl_label = "--"
            qty_label = "--"
            lock_label = "已解锁"

        today_tag = datetime.now().date().isoformat()
        today_pnl = 0.0
        if not trades_df.empty and "timestamp" in trades_df:
            today_trades = trades_df[trades_df["timestamp"].astype(str).str.startswith(today_tag)]
            for _, row in today_trades.iterrows():
                gross = float(row["price"]) * float(row["quantity"])
                if row["action"] == "BUY":
                    today_pnl -= gross + float(row["fee"])
                else:
                    today_pnl += gross - float(row["fee"])

        latest_action = snapshot.get("action") or "观望"
        total_score = float(snapshot.get("total_score") or 0.0)
        risk_text = snapshot.get("risk_text") or "空仓，暂无止损/止盈距离"
        reason = snapshot.get("reason") or logic.get("reason") or "等待下一次高质量信号。"

        positions_html = positions_df.to_html(index=False, border=0) if not positions_df.empty else "<p>无持仓</p>"
        trades_html = trades_df.head(50).to_html(index=False, border=0) if not trades_df.empty else "<p>暂无交易</p>"

        html = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>MDA 系统控制台</title>
  <style>
    :root {{
      --bg: #07111f;
      --panel: #0d1b2e;
      --card: #11233a;
      --line: #1f3b5a;
      --text: #e5edf7;
      --muted: #9db2c8;
      --accent: #7dd3fc;
      --green: #86efac;
      --amber: #fcd34d;
    }}
    body {{ font-family: "Segoe UI", sans-serif; margin: 0; background: radial-gradient(circle at top, #123051 0%, var(--bg) 48%); color: var(--text); }}
    .wrap {{ max-width: 1280px; margin: 0 auto; padding: 28px; }}
    h1, h2, h3 {{ margin: 0; }}
    .meta {{ color: var(--muted); margin: 8px 0 18px; }}
    .panel {{ background: rgba(13, 27, 46, 0.94); border: 1px solid var(--line); border-radius: 16px; padding: 18px; box-shadow: 0 18px 40px rgba(0, 0, 0, 0.22); }}
    .grid {{ display: grid; gap: 16px; }}
    .grid.top {{ grid-template-columns: 1.2fr 1fr; margin-bottom: 16px; }}
    .cards {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; margin-top: 14px; }}
    .card {{ background: var(--card); border: 1px solid var(--line); border-radius: 14px; padding: 14px; }}
    .label {{ font-size: 12px; color: var(--muted); }}
    .value {{ font-size: 22px; margin-top: 6px; font-weight: 700; color: var(--text); }}
    .score {{ font-size: 32px; color: var(--accent); font-weight: 700; }}
    .trace {{ display: grid; gap: 10px; margin-top: 12px; }}
    .trace-item {{ background: var(--card); border: 1px solid var(--line); border-radius: 12px; padding: 12px; line-height: 1.6; }}
    .action {{ font-size: 26px; color: var(--green); font-weight: 700; }}
    .warn {{ color: var(--amber); }}
    .tables {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-top: 16px; }}
    table {{ width: 100%; border-collapse: collapse; background: transparent; }}
    th, td {{ border-bottom: 1px solid var(--line); padding: 8px; text-align: left; font-size: 13px; color: var(--text); }}
    th {{ color: var(--muted); }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="panel">
      <h1>MDA 系统控制台</h1>
      <div class="meta">时间: {datetime.now().strftime("%H:%M")} | 最近快照: {snapshot.get("timestamp", "--")}</div>
      <div class="cards">
        <div class="card"><div class="label">总资产</div><div class="value">¥{account["total_assets"]:.2f}</div></div>
        <div class="card"><div class="label">可用余额</div><div class="value">¥{account["balance"]:.2f}</div></div>
        <div class="card"><div class="label">今日盈亏</div><div class="value">¥{today_pnl:.2f}</div></div>
        <div class="card"><div class="label">累计收益率</div><div class="value">{returns:+.2f}%</div></div>
      </div>
    </div>

    <div class="grid top">
      <div class="panel">
        <h2>账户与持仓状态</h2>
        <div class="cards">
          <div class="card"><div class="label">当前持仓</div><div class="value">{holding_label}</div></div>
          <div class="card"><div class="label">盈亏统计</div><div class="value">{pnl_label}</div></div>
          <div class="card"><div class="label">持股数量 / 均价</div><div class="value">{qty_label}</div></div>
          <div class="card"><div class="label">锁仓状态</div><div class="value">{snapshot.get("lock_status") or lock_label}</div></div>
        </div>
      </div>
      <div class="panel">
        <h2>执行指令与风险预警</h2>
        <div style="margin-top: 14px;" class="label">当前综合胜率估算</div>
        <div class="score">P = {total_score:.2f}%</div>
        <div style="margin-top: 12px;" class="label">风控距离</div>
        <div class="value" style="font-size: 18px;">{risk_text}</div>
        <div style="margin-top: 12px;" class="label">最终行动</div>
        <div class="action">【 {latest_action} 】</div>
        <div style="margin-top: 12px;" class="label">操作逻辑</div>
        <div class="warn">{reason}</div>
      </div>
    </div>

    <div class="panel">
      <h2>决策思考引擎 (MDA Thinking Trace)</h2>
      <div class="trace">
        <div class="trace-item"><strong>维度 1: 架构师视角</strong><br>{thinking_trace.get("data_arch") or "暂无记录"}</div>
        <div class="trace-item"><strong>维度 2: 知识库对齐</strong><br>{thinking_trace.get("notebooklm") or "暂无记录"}</div>
        <div class="trace-item"><strong>维度 3: 心理博弈论</strong><br>{thinking_trace.get("game_psych") or "暂无记录"}</div>
        <div class="trace-item"><strong>维度 4: 趋势动能</strong><br>{thinking_trace.get("trend") or "暂无记录"}</div>
      </div>
    </div>

    <div class="tables">
      <div class="panel"><h3>当前持仓明细</h3>{positions_html}</div>
      <div class="panel"><h3>最近交易记录</h3>{trades_html}</div>
    </div>
  </div>
</body>
</html>
"""

        output_path = os.path.join(output_dir, "dashboard.html")
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"✅ HTML 仪表盘已生成: {output_path}")

if __name__ == "__main__":
    rg = ReportGenerator()
    rg.generate_report()
    rg.generate_html_dashboard()
