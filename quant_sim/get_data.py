import akshare as ak
import pandas as pd
import os
import time

def get_index_data(symbol="000300", start_date="20180101", end_date="20241231"):
    """获取沪深300指数数据作为基准"""
    print(f"Fetching index data for {symbol}...")
    try:
        # 尝试使用 index_zh_a_hist (东方财富接口)
        df = ak.index_zh_a_hist(symbol=symbol, period="daily", start_date=start_date, end_date=end_date)
        if df.empty:
            raise ValueError("Empty data from index_zh_a_hist")
        df = df[['日期', '收盘']]
        df.columns = ['date', 'close']
        df['symbol'] = 'sh000300'
        df['name'] = '沪深300'
        return df
    except Exception as e:
        print(f"Error fetching index {symbol} via em: {e}, trying alternative...")
        # 备选：使用 stock_zh_index_daily (新浪接口)
        try:
            df = ak.stock_zh_index_daily(symbol="sh000300")
            df = df.reset_index()
            df.columns = ['date', 'open', 'high', 'low', 'close', 'volume']
            df = df[(df['date'] >= pd.to_datetime(start_date)) & (df['date'] <= pd.to_datetime(end_date))]
            df = df[['date', 'close']]
            df['symbol'] = 'sh000300'
            df['name'] = '沪深300'
            return df
        except Exception as e2:
            print(f"All index fetch methods failed: {e2}")
            return pd.DataFrame()

def get_stock_data(symbol="600519", start_date="20180101", end_date="20241231"):
    """获取单只股票复权数据"""
    print(f"Fetching stock data for {symbol}...")
    try:
        # 使用通用接口获取后复权数据
        df = ak.stock_zh_a_hist(symbol=symbol, period="daily", start_date=start_date, end_date=end_date, adjust="hfq")
        if df.empty:
            return pd.DataFrame()
        df = df[['日期', '收盘']]
        df.columns = ['date', 'close']
        prefix = "sh" if symbol.startswith("6") else "sz"
        df['symbol'] = f"{prefix}{symbol}"
        df['name'] = symbol 
        return df
    except Exception as e:
        print(f"Error fetching {symbol}: {e}")
        return pd.DataFrame()

def main():
    data_dir = "data"
    if not os.path.exists(data_dir):
        os.makedirs(data_dir)
        
    # 1. 获取基准
    benchmark_df = get_index_data()
    if not benchmark_df.empty:
        benchmark_df.to_csv(os.path.join(data_dir, "benchmark_hs300.csv"), index=False)
        print("Benchmark saved.")
    
    # 2. 获取几只代表性股票
    symbols = ["600519", "300750", "601318", "600036", "000858"]
    all_stocks = []
    for s in symbols:
        df = get_stock_data(s)
        if not df.empty:
            all_stocks.append(df)
        time.sleep(1)
        
    if all_stocks:
        full_df = pd.concat(all_stocks)
        full_df.to_csv(os.path.join(data_dir, "historical_quotes_5y.csv"), index=False)
        print(f"Data saved to data/historical_quotes_5y.csv, total rows: {len(full_df)}")
    else:
        print("No stock data fetched.")

if __name__ == "__main__":
    main()
