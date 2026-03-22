import logging
import pandas as pd
from datetime import datetime

from portfolio import PortfolioManager


class HistoricalBacktester:
    def __init__(self, config: dict, config_path="config.yaml", db_path="quant_sim.db"):
        self.config = config
        self.backtest_cfg = config.get("backtest", {})
        self.trading_cfg = config.get("trading", {})
        self.portfolio = PortfolioManager(config_path=config_path, db_path=db_path)
        self.max_positions = int(self.trading_cfg.get("max_positions", 3))

    def _load_data(self) -> pd.DataFrame:
        data_file = self.backtest_cfg.get("data_file", "data/historical_quotes.csv")
        df = pd.read_csv(data_file)
        required_cols = {"date", "symbol", "name", "close"}
        missing = required_cols - set(df.columns)
        if missing:
            raise ValueError(f"历史行情缺少必要字段: {missing}")

        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values(["date", "symbol"]).reset_index(drop=True)

        start_date = self.backtest_cfg.get("start_date")
        end_date = self.backtest_cfg.get("end_date")
        if start_date:
            df = df[df["date"] >= pd.to_datetime(start_date)]
        if end_date:
            df = df[df["date"] <= pd.to_datetime(end_date)]
        return df

    def _pick_candidate(self, history_window: pd.DataFrame, latest_day: pd.DataFrame):
        lookback = int(self.backtest_cfg.get("lookback_days", 5))
        win_threshold = float(self.backtest_cfg.get("min_momentum", 0.02))

        best = None
        for _, row in latest_day.iterrows():
            symbol = row["symbol"]
            closes = history_window[history_window["symbol"] == symbol]["close"].tail(lookback + 1)
            if len(closes) < lookback + 1:
                continue
            base = closes.iloc[0]
            latest = closes.iloc[-1]
            momentum = (latest / base) - 1.0
            if momentum < win_threshold:
                continue
            if best is None or momentum > best["momentum"]:
                best = {
                    "symbol": symbol,
                    "name": row["name"],
                    "price": float(latest),
                    "momentum": float(momentum),
                }
        return best

    def run(self):
        df = self._load_data()
        if df.empty:
            logging.warning("历史行情数据为空，跳过回测。")
            return

        position_pct = float(self.backtest_cfg.get("position_pct", 0.3))
        target_return = float(self.trading_cfg.get("target_return", 0.15))
        stop_loss_pct = abs(float(self.trading_cfg.get("stop_loss", -0.05)))

        all_days = sorted(df["date"].dt.date.unique())
        for day in all_days:
            now = datetime.combine(day, datetime.min.time()).replace(hour=14, minute=55)
            day_slice = df[df["date"].dt.date == day]
            day_prices = {r["symbol"]: float(r["close"]) for _, r in day_slice.iterrows()}

            self.portfolio.update_market_prices(day_prices)
            exit_actions = self.portfolio.process_exits(now=now)
            for action in exit_actions:
                logging.info(
                    "回测触发%s: %s %s, 原因: %s",
                    action["action"],
                    action["symbol"],
                    action["name"],
                    action["reason"],
                )

            positions = self.portfolio.db.get_positions()
            if len(positions) >= self.max_positions:
                continue

            account = self.portfolio.account
            if account["balance"] <= account["total_assets"] * 0.2:
                continue

            history_window = df[df["date"].dt.date <= day]
            candidate = self._pick_candidate(history_window, day_slice)
            if not candidate:
                continue

            current_price = candidate["price"]
            target_price = current_price * (1 + target_return)
            stop_loss_price = current_price * (1 - stop_loss_pct)
            reason = f"Backtest momentum strategy ({candidate['momentum']*100:.2f}%)"

            self.portfolio.buy(
                symbol=candidate["symbol"],
                name=candidate["name"],
                price=current_price,
                position_pct=position_pct,
                target_price=target_price,
                stop_loss_price=stop_loss_price,
                reason=reason,
                trade_time=now,
            )

        logging.info("历史回测执行完成。")
