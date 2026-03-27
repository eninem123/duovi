import logging
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import seaborn as sns
import yaml
import os
from datetime import datetime, timedelta
from portfolio import PortfolioManager
from risk_gate import RiskGate

# 设置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

class HistoricalBacktester:
    def __init__(self, config_path="config.yaml", db_path="backtest.db"):
        if os.path.exists(db_path):
            os.remove(db_path) # 每次回测前清理旧数据库
            
        with open(config_path, "r", encoding="utf-8") as f:
            self.config = yaml.safe_load(f)
        self.backtest_cfg = self.config.get("backtest", {})
        self.trading_cfg = self.config.get("trading", {})
        
        # 初始化 PortfolioManager
        self.portfolio = PortfolioManager(config_path=config_path, db_path=db_path)
        self.risk_gate = RiskGate(self.config)
        
        # 结果存储
        self.equity_curve = []
        self.benchmark_curve = []
        self.trade_log = []

    def _load_data(self) -> pd.DataFrame:
        data_file = self.backtest_cfg.get("data_file", "data/historical_quotes_5y.csv")
        try:
            df = pd.read_csv(data_file)
        except FileNotFoundError:
            logging.error(f"找不到历史行情文件: {data_file}")
            return pd.DataFrame()
            
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values(["date", "symbol"]).reset_index(drop=True)

        start_date = self.backtest_cfg.get("start_date", "2018-01-01")
        end_date = self.backtest_cfg.get("end_date", "2024-12-31")
        df = df[(df["date"] >= pd.to_datetime(start_date)) & (df["date"] <= pd.to_datetime(end_date))]
        return df

    def _load_benchmark(self) -> pd.DataFrame:
        benchmark_file = "data/benchmark_hs300.csv"
        try:
            df = pd.read_csv(benchmark_file)
            df["date"] = pd.to_datetime(df["date"])
            return df.sort_values("date")
        except:
            logging.warning("Benchmark data not found, skipping benchmark comparison.")
            return pd.DataFrame()

    def _pick_candidate(self, history_window: pd.DataFrame, latest_day: pd.DataFrame):
        lookback = int(self.backtest_cfg.get("lookback_days", 20))
        win_threshold = float(self.backtest_cfg.get("min_momentum", 0.02)) # 调低阈值以产生交易

        candidates = []
        for _, row in latest_day.iterrows():
            symbol = row["symbol"]
            closes = history_window[history_window["symbol"] == symbol]["close"].tail(lookback + 1)
            if len(closes) < lookback + 1:
                continue
            base = closes.iloc[0]
            latest = closes.iloc[-1]
            momentum = (latest / base) - 1.0
            if momentum >= win_threshold:
                candidates.append({
                    "symbol": symbol,
                    "name": row["name"],
                    "price": float(latest),
                    "momentum": float(momentum),
                })
        
        if not candidates:
            return None
        # 按动量排序，取最强的
        candidates.sort(key=lambda x: x["momentum"], reverse=True)
        return candidates[0]

    def run(self):
        df = self._load_data()
        benchmark_df = self._load_benchmark()
        if df.empty:
            logging.warning("历史行情数据为空，跳过回测。")
            return

        position_pct = float(self.backtest_cfg.get("position_pct", 0.3))
        target_return = float(self.trading_cfg.get("target_return", 0.15))
        stop_loss_pct = abs(float(self.trading_cfg.get("stop_loss", -0.05)))
        
        # 滑点模拟：买入价增加 0.1%，卖出价减少 0.1%
        slippage = 0.001 

        all_days = sorted(df["date"].dt.date.unique())
        
        for day in all_days:
            now = datetime.combine(day, datetime.min.time()).replace(hour=14, minute=55)
            day_slice = df[df["date"].dt.date == day]
            day_prices = {r["symbol"]: float(r["close"]) for _, r in day_slice.iterrows()}

            # 1. 更新价格并处理卖出
            self.portfolio.update_market_prices(day_prices)
            
            # 模拟卖出滑点
            positions = self.portfolio.db.get_positions()
            for p in positions:
                if p['symbol'] in day_prices:
                    original_price = day_prices[p['symbol']]
                    day_prices[p['symbol']] = original_price * (1 - slippage)
            
            exit_actions = self.portfolio.process_exits(now=now)
            
            # 2. 记录每日资产
            acc = self.portfolio.db.get_account()
            self.equity_curve.append({
                "date": day,
                "total_assets": acc["total_assets"],
                "cash": acc["balance"]
            })
            
            # 记录基准
            b_val = benchmark_df[benchmark_df["date"].dt.date == day]
            if not b_val.empty:
                self.benchmark_curve.append({
                    "date": day,
                    "close": b_val.iloc[0]["close"]
                })

            # 3. 检查买入门禁
            positions = self.portfolio.db.get_positions()
            if self.risk_gate.buy_blocked_reason(None, len(positions)):
                continue

            # 4. 寻找候选标的
            history_window = df[df["date"].dt.date <= day]
            candidate = self._pick_candidate(history_window, day_slice)
            if not candidate:
                continue

            # 5. 执行买入（含滑点）
            buy_price = candidate["price"] * (1 + slippage)
            target_price = buy_price * (1 + target_return)
            stop_loss_price = buy_price * (1 - stop_loss_pct)
            
            self.portfolio.buy(
                symbol=candidate["symbol"],
                name=candidate["name"],
                price=buy_price,
                position_pct=position_pct,
                target_price=target_price,
                stop_loss_price=stop_loss_price,
                reason=f"Momentum {candidate['momentum']:.2%}",
                trade_time=now,
            )

        self.analyze_results()

    def analyze_results(self):
        if not self.equity_curve:
            return
            
        equity_df = pd.DataFrame(self.equity_curve)
        equity_df["date"] = pd.to_datetime(equity_df["date"])
        equity_df.set_index("date", inplace=True)
        
        # 计算收益率
        equity_df["returns"] = equity_df["total_assets"].pct_change().fillna(0)
        equity_df["cum_returns"] = (1 + equity_df["returns"]).cumprod() - 1
        
        # 计算基准收益率
        if self.benchmark_curve:
            bench_df = pd.DataFrame(self.benchmark_curve)
            bench_df["date"] = pd.to_datetime(bench_df["date"])
            bench_df.set_index("date", inplace=True)
            bench_df["returns"] = bench_df["close"].pct_change().fillna(0)
            bench_df["cum_returns"] = (1 + bench_df["returns"]).cumprod() - 1
            # 对齐
            equity_df["benchmark_cum_returns"] = bench_df["cum_returns"]
            equity_df["benchmark_returns"] = bench_df["returns"]

        # 核心指标
        total_return = equity_df["cum_returns"].iloc[-1]
        days = (equity_df.index[-1] - equity_df.index[0]).days
        annual_return = (1 + total_return) ** (365.0 / days) - 1
        
        volatility = equity_df["returns"].std() * np.sqrt(244)
        risk_free_rate = 0.02
        sharpe = (annual_return - risk_free_rate) / volatility if volatility != 0 else 0
        
        rolling_max = equity_df["total_assets"].cummax()
        drawdown = (equity_df["total_assets"] - rolling_max) / rolling_max
        max_drawdown = drawdown.min()
        calmar = annual_return / abs(max_drawdown) if max_drawdown != 0 else 0
        
        print("\n" + "="*30)
        print("   BACKTEST PERFORMANCE")
        print("="*30)
        print(f"Total Return:     {total_return:.2%}")
        print(f"Annual Return:    {annual_return:.2%}")
        print(f"Max Drawdown:     {max_drawdown:.2%}")
        print(f"Sharpe Ratio:     {sharpe:.2f}")
        print(f"Calmar Ratio:     {calmar:.2f}")
        
        # 市场状态表现
        if "benchmark_returns" in equity_df:
            # 定义市场状态：基准收益率 > 0.5% 牛市， < -0.5% 熊市，其他震荡
            equity_df['market_state'] = 'Vibrate'
            equity_df.loc[equity_df['benchmark_returns'] > 0.005, 'market_state'] = 'Bull'
            equity_df.loc[equity_df['benchmark_returns'] < -0.005, 'market_state'] = 'Bear'
            
            state_perf = equity_df.groupby('market_state')['returns'].mean() * 244
            print("\nMarket State Performance (Annualized):")
            for state, perf in state_perf.items():
                print(f"{state}: {perf:.2%}")
        
        self.plot_results(equity_df, drawdown)

    def plot_results(self, equity_df, drawdown):
        plt.figure(figsize=(15, 15))
        
        # 1. 累计收益曲线
        plt.subplot(4, 1, 1)
        plt.plot(equity_df.index, equity_df["cum_returns"] * 100, label="Strategy", color='blue')
        if "benchmark_cum_returns" in equity_df:
            plt.plot(equity_df.index, equity_df["benchmark_cum_returns"] * 100, label="Benchmark (HS300)", color='gray', linestyle='--')
        plt.title("Cumulative Returns (%)")
        plt.legend()
        plt.grid(True, alpha=0.3)

        # 2. 回撤曲线
        plt.subplot(4, 1, 2)
        plt.fill_between(equity_df.index, drawdown * 100, 0, color='red', alpha=0.3)
        plt.title("Drawdown (%)")
        plt.grid(True, alpha=0.3)

        # 3. 月度收益热力图
        plt.subplot(4, 1, 3)
        monthly_returns = equity_df["returns"].resample('ME').apply(lambda x: (1 + x).prod() - 1)
        monthly_df = pd.DataFrame({
            'Year': monthly_returns.index.year,
            'Month': monthly_returns.index.month,
            'Return': monthly_returns.values
        })
        pivot_table = monthly_df.pivot(index='Year', columns='Month', values='Return')
        sns.heatmap(pivot_table, annot=True, fmt=".1%", cmap="RdYlGn", center=0)
        plt.title("Monthly Returns Heatmap")

        # 4. 市场状态表现
        if 'market_state' in equity_df:
            plt.subplot(4, 1, 4)
            state_perf = equity_df.groupby('market_state')['returns'].mean() * 244
            state_perf.plot(kind='bar', color=['red', 'green', 'blue'])
            plt.title("Annualized Return by Market State")
            plt.ylabel("Return")

        plt.tight_layout()
        plt.savefig("backtest_report.png")
        print("\nReport saved as backtest_report.png")

if __name__ == "__main__":
    tester = HistoricalBacktester()
    tester.run()
