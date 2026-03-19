import sqlite3
import pandas as pd
import matplotlib.pyplot as plt
import os

class ReportGenerator:
    def __init__(self, db_path="quant_sim.db"):
        self.db_path = db_path
        
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

if __name__ == "__main__":
    rg = ReportGenerator()
    rg.generate_report()
