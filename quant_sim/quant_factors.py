import pandas as pd
import numpy as np
import logging
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)

class QuantFactors:
    def __init__(self, config: Dict[str, Any]):
        self.config = config.get("factors", {})
        self.momentum_cfg = self.config.get("momentum", {})
        self.valuation_cfg = self.config.get("valuation", {})
        self.liquidity_cfg = self.config.get("liquidity", {})
        self.technical_cfg = self.config.get("technical", {})

    def calculate_momentum(self, historical_df: pd.DataFrame) -> pd.DataFrame:
        """
        计算动量因子。
        historical_df 预期包含: [date, symbol, close]
        """
        if historical_df.empty:
            return pd.DataFrame()

        lookback_days = self.momentum_cfg.get("lookback_days", [5, 10, 20])
        min_rank_percentile = self.momentum_cfg.get("min_rank_percentile", 70)

        df = historical_df.copy()
        df = df.sort_values(['symbol', 'date'])
        
        momentum_cols = []
        for days in lookback_days:
            col_name = f'return_{days}d'
            df[col_name] = df.groupby('symbol')['close'].pct_change(periods=days)
            momentum_cols.append(col_name)
        
        # 取最新日期的动量
        latest_date = df['date'].max()
        latest_df = df[df['date'] == latest_date].copy()
        
        # 综合动量评分（平均涨幅）
        latest_df['momentum_score'] = latest_df[momentum_cols].mean(axis=1)
        # 计算全市场百分比排名
        latest_df['momentum_rank'] = latest_df['momentum_score'].rank(pct=True)
        
        latest_df['passed_momentum'] = latest_df['momentum_rank'] >= (min_rank_percentile / 100)
        return latest_df[['symbol', 'passed_momentum', 'momentum_score', 'momentum_rank']]

    def calculate_valuation(self, fundamentals_df: pd.DataFrame) -> pd.DataFrame:
        """
        计算估值因子。
        fundamentals_df 预期包含: [symbol, pe_ttm, pb_mrq]
        """
        if fundamentals_df.empty:
            return pd.DataFrame()

        pe_max_p = self.valuation_cfg.get("pe_percentile_max", 60)
        pb_max_p = self.valuation_cfg.get("pb_percentile_max", 50)

        df = fundamentals_df.copy()
        # 简化处理：使用当前候选池内的百分比排名
        df['pe_rank'] = df['pe_ttm'].rank(pct=True)
        df['pb_rank'] = df['pb_mrq'].rank(pct=True)

        df['passed_valuation'] = (df['pe_rank'] <= (pe_max_p / 100)) & \
                                 (df['pb_rank'] <= (pb_max_p / 100))
        return df[['symbol', 'passed_valuation', 'pe_rank', 'pb_rank']]

    def calculate_liquidity(self, market_df: pd.DataFrame) -> pd.DataFrame:
        """
        计算流动性因子。
        market_df 预期包含: [symbol, turnover_20d, bid_ask_spread_pct]
        """
        if market_df.empty:
            return pd.DataFrame()

        min_turnover = self.liquidity_cfg.get("min_avg_turnover_20d", 50000000)
        max_spread = self.liquidity_cfg.get("max_bid_ask_spread_pct", 0.005)

        df = market_df.copy()
        df['passed_liquidity'] = (df['turnover_20d'] >= min_turnover) & \
                                 (df['bid_ask_spread_pct'] <= max_spread)
        return df[['symbol', 'passed_liquidity']]

    def calculate_technical(self, tech_df: pd.DataFrame) -> pd.DataFrame:
        """
        计算技术面因子。
        tech_df 预期包含: [symbol, ma5, ma10, ma20, volume_ratio]
        """
        if tech_df.empty:
            return pd.DataFrame()

        ma_aligned_req = self.technical_cfg.get("ma_trend_aligned", True)
        min_vol_ratio = self.technical_cfg.get("volume_ratio_min", 1.5)

        df = tech_df.copy()
        if ma_aligned_req:
            df['ma_aligned'] = (df['ma5'] > df['ma10']) & (df['ma10'] > df['ma20'])
        else:
            df['ma_aligned'] = True
        
        df['passed_technical'] = df['ma_aligned'] & (df['volume_ratio'] >= min_vol_ratio)
        return df[['symbol', 'passed_technical']]

    async def screen_candidates(self, universe_data: Dict[str, Any]) -> List[str]:
        """
        综合所有因子进行筛选。
        universe_data 结构: { 'symbol': { 'historical': df, 'fundamentals': dict, 'market': dict, 'technical': dict } }
        """
        if not universe_data:
            return []

        all_results = []
        for symbol, data in universe_data.items():
            # 这里的逻辑在实际集成时会根据 MCP 返回的数据格式进行解析
            # 目前先定义接口契约
            pass
        
        # 简化版实现：假设输入已经是清洗好的各维度 DataFrame
        # 实际在 mcp_agent.py 中调用
        return []
