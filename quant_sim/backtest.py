import yaml
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import seaborn as sns
from datetime import datetime, timedelta
import logging
import os

from quant_sim.portfolio import PortfolioManager
from quant_sim.risk_gate import RiskGate
from quant_sim.update_kb import main as update_kb_main, KnowledgeBaseUpdater

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

class HistoricalBacktester:
    def __init__(self, config_path="config.yaml"):
        script_dir = os.path.dirname(__file__)
        config_full_path = os.path.join(script_dir, config_path)
        with open(config_full_path, "r", encoding="utf-8") as f:
            self.config = yaml.safe_load(f)

        self.backtest_cfg = self.config.get("backtest", {})
        self.trading_cfg = self.config.get("trading", {})

        # 初始化 Qdrant 客户端和 Embedding 模型 (只在这里初始化一次)
        self.qdrant_client, self.embedding_model = update_kb_main()
        
        # 创建 KnowledgeBaseUpdater 实例并更新知识库
        updater = KnowledgeBaseUpdater(config_path=config_path, qdrant_client=self.qdrant_client, embedding_model=self.embedding_model)
        updater.update_knowledge_base()

        self.portfolio = PortfolioManager(config_path=config_path)
        # 将已初始化的 Qdrant 客户端和 Embedding 模型传递给 RiskGate
        self.risk_gate = RiskGate(config=self.config, qdrant_client=self.qdrant_client, embedding_model=self.embedding_model)

        self.daily_metrics = []
        self.transactions = []
        self.load_data()

    def load_data(self):
        logging.info("Loading historical data...")
        self.stock_data = pd.read_csv(self.config["data"]["historical_quotes_path"], index_col=0, parse_dates=True)
        self.stock_data = self.stock_data.loc[self.backtest_cfg["start_date"]:self.backtest_cfg["end_date"]]

        self.benchmark_data = pd.read_csv(self.config["data"]["benchmark_quotes_path"], index_col=0, parse_dates=True)
        self.benchmark_data = self.benchmark_data.loc[self.backtest_cfg["start_date"]:self.backtest_cfg["end_date"]]
        logging.info("Historical data loaded.")

    def calculate_momentum(self, data):
        # 计算动量因子
        return data["close"].pct_change(periods=self.config["strategy"]["momentum_window"]).iloc[-1]

    def get_market_regime(self, current_date):
        # 简化市场状态判断：基于基准指数过去N天的涨跌幅
        lookback_days = 60 # 两个月
        if len(self.benchmark_data.loc[:current_date]) < lookback_days:
            return "range_bound"
        
        recent_benchmark = self.benchmark_data.loc[:current_date].iloc[-lookback_days:]
        change = (recent_benchmark["close"].iloc[-1] - recent_benchmark["close"].iloc[0]) / recent_benchmark["close"].iloc[0]

        if change > 0.10: # 涨幅超过10%视为牛市
            return "bull"
        elif change < -0.10: # 跌幅超过10%视为熊市
            return "bear"
        else:
            return "range_bound"

    def run_backtest(self):
        logging.info("Starting backtest...")
        dates = self.stock_data.index.unique().sort_values()

        for current_date in dates:
            current_date_str = current_date.strftime("%Y-%m-%d")
            logging.info(f"Processing date: {current_date_str}")

            daily_data = self.stock_data.loc[current_date]
            if isinstance(daily_data, pd.Series): # Handle single stock case
                daily_data = pd.DataFrame([daily_data])
            daily_data = daily_data.set_index("code")

            # 更新持仓状态和检查止损止盈
            self.portfolio.update_positions(current_date, daily_data)

            # 获取市场状态
            market_regime = self.get_market_regime(current_date)
            self.portfolio.set_market_regime(market_regime)

            # 卖出逻辑
            for stock_code in list(self.portfolio.positions.keys()): # Iterate over a copy
                position = self.portfolio.positions[stock_code]
                current_price = daily_data.loc[stock_code]["close"] if stock_code in daily_data.index else None

                if current_price is None:
                    logging.warning(f"[{stock_code}] {current_date_str}: 无法获取最新价格，跳过卖出检查。")
                    continue

                # 止损
                if (current_price - position["buy_price"]) / position["buy_price"] < self.config["strategy"]["stop_loss"]:
                    self.portfolio.sell(stock_code, current_price, "Stop Loss", self.config["trade_costs"]["slippage_rate"], self.config["trade_costs"]["commission_rate"], self.config["trade_costs"]["stamp_tax_rate"])
                    self.transactions.append({"date": current_date, "code": stock_code, "type": "SELL", "price": current_price, "shares": position["shares"], "reason": "Stop Loss"})
                    continue

                # 止盈
                if (current_price - position["buy_price"]) / position["buy_price"] > self.config["strategy"]["target_return"]:
                    self.portfolio.sell(stock_code, current_price, "Target Return", self.config["trade_costs"]["slippage_rate"], self.config["trade_costs"]["commission_rate"], self.config["trade_costs"]["stamp_tax_rate"])
                    self.transactions.append({"date": current_date, "code": stock_code, "type": "SELL", "price": current_price, "shares": position["shares"], "reason": "Target Return"})
                    continue
                
                # 最大持仓天数
                if (current_date - position["buy_date"]).days > self.config["strategy"]["max_holding_days"]:
                    self.portfolio.sell(stock_code, current_price, "Max Holding Days", self.config["trade_costs"]["slippage_rate"], self.config["trade_costs"]["commission_rate"], self.config["trade_costs"]["stamp_tax_rate"])
                    self.transactions.append({"date": current_date, "code": stock_code, "type": "SELL", "price": current_price, "shares": position["shares"], "reason": "Max Holding Days"})
                    continue

            # 买入逻辑
            candidate_stocks = []
            for stock_code in daily_data.index:
                if stock_code not in self.portfolio.positions and len(self.stock_data.loc[self.stock_data['code'] == stock_code]) > self.config["strategy"]["momentum_window"]:
                    stock_momentum = self.calculate_momentum(self.stock_data.loc[self.stock_data['code'] == stock_code])
                    if stock_momentum > self.config["strategy"]["min_momentum"]:
                        candidate_stocks.append((stock_code, stock_momentum))
            
            # 按动量从高到低排序
            candidate_stocks.sort(key=lambda x: x[1], reverse=True)

            for stock_code, momentum in candidate_stocks:
                if len(self.portfolio.positions) < self.config["strategy"]["max_positions"]:
                    current_price = daily_data.loc[stock_code]["close"]
                    
                    # 风险门禁检查
                    blocked_reason = self.risk_gate.buy_blocked_reason(stock_code, current_date_str, current_price)
                    if blocked_reason:
                        logging.warning(f"[{stock_code}] {current_date_str}: {blocked_reason}")
                        continue

                    # 尝试买入
                    shares_to_buy = self.portfolio.calculate_shares_to_buy(stock_code, current_price)
                    if shares_to_buy > 0:
                        self.portfolio.buy(stock_code, current_price, shares_to_buy, current_date, self.config["trade_costs"]["slippage_rate"], self.config["trade_costs"]["commission_rate"], self.config["trade_costs"]["stamp_tax_rate"])
                        self.transactions.append({"date": current_date, "code": stock_code, "type": "BUY", "price": current_price, "shares": shares_to_buy, "reason": "Momentum"})

            # 记录每日指标
            current_portfolio_value = self.portfolio.get_portfolio_value(current_date, daily_data)
            benchmark_value = self.benchmark_data.loc[current_date]["close"] if current_date in self.benchmark_data.index else np.nan
            self.daily_metrics.append({
                "date": current_date,
                "portfolio_value": current_portfolio_value,
                "cash": self.portfolio.cash,
                "total_assets": self.portfolio.total_assets,
                "benchmark_value": benchmark_value
            })

        logging.info("Backtest finished.")
        self.daily_metrics_df = pd.DataFrame(self.daily_metrics).set_index("date")
        self.daily_metrics_df["portfolio_return"] = self.daily_metrics_df["portfolio_value"].pct_change().fillna(0)
        self.daily_metrics_df["benchmark_return"] = self.daily_metrics_df["benchmark_value"].pct_change().fillna(0)
        self.daily_metrics_df["cumulative_portfolio_return"] = (1 + self.daily_metrics_df["portfolio_return"]).cumprod() - 1
        self.daily_metrics_df["cumulative_benchmark_return"] = (1 + self.daily_metrics_df["benchmark_return"]).cumprod() - 1

    def analyze_results(self):
        if self.daily_metrics_df.empty:
            logging.warning("No daily metrics to analyze.")
            return

        # 年化收益
        total_days = (self.daily_metrics_df.index[-1] - self.daily_metrics_df.index[0]).days
        annual_factor = 252 / total_days # 假设每年252个交易日
        annual_portfolio_return = (1 + self.daily_metrics_df["portfolio_return"]).prod()**(annual_factor) - 1
        annual_benchmark_return = (1 + self.daily_metrics_df["benchmark_return"]).prod()**(annual_factor) - 1

        # 年化波动率
        annual_portfolio_volatility = self.daily_metrics_df["portfolio_return"].std() * np.sqrt(252)
        annual_benchmark_volatility = self.daily_metrics_df["benchmark_return"].std() * np.sqrt(252)

        # 夏普比率 (假设无风险利率为0)
        sharpe_ratio = annual_portfolio_return / annual_portfolio_volatility if annual_portfolio_volatility != 0 else 0

        # 最大回撤
        peak = self.daily_metrics_df["cumulative_portfolio_return"].expanding(min_periods=1).max()
        drawdown = (self.daily_metrics_df["cumulative_portfolio_return"] - peak) / (peak + 1)
        max_drawdown = drawdown.min()

        # 卡玛比率
        calmar_ratio = annual_portfolio_return / abs(max_drawdown) if abs(max_drawdown) != 0 else 0

        # Alpha (简化计算，假设Beta为1)
        alpha = annual_portfolio_return - annual_benchmark_return

        logging.info("\n--- 回测结果概览 ---")
        logging.info(f"初始资金: {self.config['backtest']['initial_capital']:.2f}")
        logging.info(f'最终资产: {self.daily_metrics_df["total_assets"].iloc[-1]:.2f}')
        logging.info(f'总收益率: {self.daily_metrics_df["cumulative_portfolio_return"].iloc[-1]:.2%}')
        logging.info(f'年化收益率: {annual_portfolio_return:.2%}')
        logging.info(f'基准年化收益率: {annual_benchmark_return:.2%}')
        logging.info(f'年化波动率: {annual_portfolio_volatility:.2%}')
        logging.info(f'夏普比率: {sharpe_ratio:.2f}')
        logging.info(f'最大回撤: {max_drawdown:.2%}')
        logging.info(f'卡玛比率: {calmar_ratio:.2f}')
        logging.info(f'Alpha: {alpha:.2%}')

        # 月度收益热力图
        monthly_returns = self.daily_metrics_df["portfolio_return"].resample("M").apply(lambda x: (1 + x).prod() - 1)
        monthly_returns_df = pd.DataFrame({
            "year": monthly_returns.index.year,
            "month": monthly_returns.index.month,
            "return": monthly_returns
        })
        monthly_returns_pivot = monthly_returns_df.pivot("year", "month", "return")

        plt.figure(figsize=(12, 8))
        sns.heatmap(monthly_returns_pivot, annot=True, fmt=".2%", cmap="RdYlGn", center=0)
        plt.title("月度收益热力图")
        plt.xlabel("月份")
        plt.ylabel("年份")
        plt.tight_layout()
        plt.savefig("monthly_returns_heatmap.png")
        plt.close()

        # 累计收益曲线
        plt.figure(figsize=(12, 6))
        plt.plot(self.daily_metrics_df.index, self.daily_metrics_df["cumulative_portfolio_return"], label='策略累计收益')
        plt.plot(self.daily_metrics_df.index, self.daily_metrics_df["cumulative_benchmark_return"], label='基准累计收益 (沪深300)')
        plt.title('策略与基准累计收益曲线')
        plt.xlabel('日期')
        plt.ylabel('累计收益率')
        plt.legend()
        plt.grid(True)
        plt.tight_layout()
        plt.savefig("cumulative_returns.png")
        plt.close()

        # 回撤曲线
        plt.figure(figsize=(12, 6))
        plt.plot(self.daily_metrics_df.index, drawdown, label='策略回撤')
        plt.title('策略回撤曲线')
        plt.xlabel('日期')
        plt.ylabel('回撤')
        plt.legend()
        plt.grid(True)
        plt.tight_layout()
        plt.savefig("drawdown_curve.png")
        plt.close()

        # 市场状态表现
        market_regime_returns = {"bull": [], "bear": [], "range_bound": []}
        for date, row in self.daily_metrics_df.iterrows():
            regime = self.get_market_regime(date)
            market_regime_returns[regime].append(row["portfolio_return"])
        
        logging.info("\n--- 分市场状态表现 (年化收益) ---")
        for regime, returns in market_regime_returns.items():
            if returns:
                total_regime_days = len(returns)
                regime_annual_factor = 252 / total_regime_days if total_regime_days > 0 else 0
                annual_regime_return = (1 + pd.Series(returns)).prod()**(regime_annual_factor) - 1 if regime_annual_factor > 0 else 0
                logging.info(f'{regime.capitalize()}市场: {annual_regime_return:.2%}')
            else:
                logging.info(f'{regime.capitalize()}市场: 无数据')

    def plot_results(self):
        # 合并所有图表到一个报告图片
        fig, axes = plt.subplots(nrows=4, ncols=1, figsize=(15, 25))
        
        # 累计收益曲线
        axes[0].plot(self.daily_metrics_df.index, self.daily_metrics_df["cumulative_portfolio_return"], label='策略累计收益')
        axes[0].plot(self.daily_metrics_df.index, self.daily_metrics_df["cumulative_benchmark_return"], label='基准累计收益 (沪深300)')
        axes[0].set_title('策略与基准累计收益曲线')
        axes[0].set_xlabel('日期')
        axes[0].set_ylabel('累计收益率')
        axes[0].legend()
        axes[0].grid(True)

        # 回撤曲线
        peak = self.daily_metrics_df["cumulative_portfolio_return"].expanding(min_periods=1).max()
        drawdown = (self.daily_metrics_df["cumulative_portfolio_return"] - peak) / (peak + 1)
        axes[1].plot(self.daily_metrics_df.index, drawdown, label='策略回撤')
        axes[1].set_title('策略回撤曲线')
        axes[1].set_xlabel('日期')
        axes[1].set_ylabel('回撤')
        axes[1].legend()
        axes[1].grid(True)

        # 月度收益热力图
        monthly_returns = self.daily_metrics_df["portfolio_return"].resample("M").apply(lambda x: (1 + x).prod() - 1)
        monthly_returns_df = pd.DataFrame({
            'year': monthly_returns.index.year,
            'month': monthly_returns.index.month,
            'return': monthly_returns
        })
        monthly_returns_pivot = monthly_returns_df.pivot("year", "month", "return")
        sns.heatmap(monthly_returns_pivot, annot=True, fmt=".2%", cmap="RdYlGn", center=0, ax=axes[2])
        axes[2].set_title("月度收益热力图")
        axes[2].set_xlabel("月份")
        axes[2].set_ylabel("年份")

        # 每日资产价值
        axes[3].plot(self.daily_metrics_df.index, self.daily_metrics_df["total_assets"], label='总资产')
        axes[3].set_title('每日总资产价值')
        axes[3].set_xlabel('日期')
        axes[3].set_ylabel('资产价值')
        axes[3].legend()
        axes[3].grid(True)

        plt.tight_layout()
        plt.savefig("backtest_report.png")
        plt.close()


if __name__ == "__main__":
    tester = HistoricalBacktester()
    tester.run_backtest()
    tester.analyze_results()
    tester.plot_results()
