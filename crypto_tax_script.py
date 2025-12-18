import pandas as pd
import yfinance as yf
from decimal import Decimal, ROUND_HALF_UP
import datetime
import os
import sys

### USER CONFIGURATION ###
COINBASE_FILE_PATH = 'coinbase.csv'
BINANCE_FILE_PATH = 'binance.csv'
OUTPUT_FILE_NAME = 'master_crypto_ledger.csv'

# Configure logging for clear console output
import logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def fetch_fx_history():
    """
    Fetches the full history of GBPUSD=X from Yahoo Finance.
    Returns a Series with Date index and Close price.
    """
    logger.info("Fetching historical FX rates for GBPUSD=X...")
    try:
        # GBPUSD=X: Returns USD per 1 GBP.
        ticker = "GBPUSD=X"
        # Fetch MAX history to ensure we cover all dates
        data = yf.download(ticker, period="max", progress=False, auto_adjust=False)
        if data.empty:
            logger.error("Failed to fetch FX data. Returned empty dataset.")
            sys.exit(1)

        # Handle potential MultiIndex in columns
        if isinstance(data.columns, pd.MultiIndex):
            # If MultiIndex (Price, Ticker), we want (Close, Ticker) or just Close
            if ticker in data.columns.get_level_values(1):
                 fx_series = data['Close'][ticker]
            else:
                 # Fallback if ticker structure is different
                 fx_series = data['Close'].iloc[:, 0]
        else:
            fx_series = data['Close']

        # Ensure index is datetime and normalized to date (midnight)
        fx_series.index = pd.to_datetime(fx_series.index).normalize()

        # Sort index to ensure asof works
        fx_series = fx_series.sort_index()

        return fx_series
    except Exception as e:
        logger.error(f"Error fetching FX history: {e}")
        sys.exit(1)

def get_fx_rate(date_obj, fx_history):
    """
    Helper to look up rate for a given date.
    Uses 'asof' to find the most recent rate if exact match is missing (e.g. weekends).
    """
    # Normalize date to midnight and strip timezone to match fx_history index
    target_date = pd.Timestamp(date_obj).tz_localize(None).normalize()

    # asof returns the value at or before the label
    try:
        rate = fx_history.asof(target_date)
        if pd.isna(rate):
            # If date is before the start of history
            logger.warning(f"Date {target_date.date()} is before start of FX history ({fx_history.index.min().date()}). Using first available.")
            return fx_history.iloc[0]
        return rate
    except Exception as e:
        logger.warning(f"Error looking up FX rate for {target_date}: {e}. Defaulting to 1.0 (Safety).")
        return 1.0

def safe_decimal_conversion(val):
    """
    Safely converts a value to Decimal, handling commas and NaNs.
    """
    s = str(val).replace(',', '')
    try:
        return Decimal(s)
    except:
        return Decimal(0)

def parse_coinbase(filepath):
    """
    Parses Coinbase CSV.
    """
    if not os.path.exists(filepath):
        logger.warning(f"Coinbase file not found at: {filepath}. Skipping.")
        return pd.DataFrame()

    logger.info(f"Processing Coinbase file: {filepath}")
    try:
        df = pd.read_csv(filepath)

        required_cols = ['Timestamp', 'Transaction Type', 'Asset', 'Quantity Transacted', 'Total (inclusive of fees and/or spread)']
        missing = [c for c in required_cols if c not in df.columns]
        if missing:
            logger.error(f"Coinbase file missing columns: {missing}")
            return pd.DataFrame()

        df['TIME_UTC'] = pd.to_datetime(df['Timestamp'], utc=True)

        df = df.rename(columns={
            'Transaction Type': 'TXN_TYPE_RAW',
            'Asset': 'COIN',
            'Quantity Transacted': 'QTY',
            'Total (inclusive of fees and/or spread)': 'TOTAL_USD'
        })

        df['EXCHANGE'] = 'Coinbase'

        keep_map = {
            'Advance Trade Buy': 'BUY',
            'Advanced Trade Buy': 'BUY',
            'Buy': 'BUY',
            'Advance Trade Sell': 'SELL',
            'Advanced Trade Sell': 'SELL',
            'Sell': 'SELL',
            'Reward Income': 'INCOME',
            'Staking Income': 'INCOME'
        }

        df = df[df['TXN_TYPE_RAW'].isin(keep_map.keys())].copy()
        df['TXN_TYPE'] = df['TXN_TYPE_RAW'].map(keep_map)

        return df[['EXCHANGE', 'TIME_UTC', 'TXN_TYPE', 'COIN', 'QTY', 'TOTAL_USD']]

    except Exception as e:
        logger.error(f"Error parsing Coinbase file: {e}")
        return pd.DataFrame()

def parse_binance(filepath):
    """
    Parses Binance CSV.
    """
    if not os.path.exists(filepath):
        logger.warning(f"Binance file not found at: {filepath}. Skipping.")
        return pd.DataFrame()

    logger.info(f"Processing Binance file: {filepath}")
    try:
        df = pd.read_csv(filepath)

        required_cols = ['Date(UTC)', 'Base Asset', 'Quote Asset', 'Type', 'Amount', 'Total']
        missing = [c for c in required_cols if c not in df.columns]
        if missing:
            logger.error(f"Binance file missing columns: {missing}")
            return pd.DataFrame()

        df['TIME_UTC'] = pd.to_datetime(df['Date(UTC)'], utc=True)

        df = df.rename(columns={
            'Base Asset': 'COIN',
            'Quote Asset': 'CCY',
            'Amount': 'QTY',
            'Total': 'TOTAL_USD_RAW'
        })

        df['EXCHANGE'] = 'Binance'

        keep_map = {'Buy': 'BUY', 'Sell': 'SELL'}
        df = df[df['Type'].isin(keep_map.keys())].copy()
        df['TXN_TYPE'] = df['Type'].map(keep_map)

        # USDT Fix: Treat USDT as USD.
        # Strict logic: "If CCY column is 'USDT', treat it as 'USD'."
        # This implies we keep rows where CCY is USD or USDT.
        valid_ccy = ['USD', 'USDT']
        df = df[df['CCY'].isin(valid_ccy)].copy()

        df = df.rename(columns={'TOTAL_USD_RAW': 'TOTAL_USD'})

        return df[['EXCHANGE', 'TIME_UTC', 'TXN_TYPE', 'COIN', 'QTY', 'TOTAL_USD']]

    except Exception as e:
        logger.error(f"Error parsing Binance file: {e}")
        return pd.DataFrame()

def apply_signage_rules(row):
    """
    Applies sign enforcement logic.
    Returns modified QTY, TOTAL_USD, TOTAL_GBP (as Decimals).
    """
    txn_type = row['TXN_TYPE']

    # Ensure they are Decimals
    qty = row['QTY']
    total_usd = row['TOTAL_USD']
    total_gbp = row['TOTAL_GBP']

    # Absolute values first to reset
    qty = abs(qty)
    total_usd = abs(total_usd)
    total_gbp = abs(total_gbp)

    if txn_type in ['BUY', 'INCOME']:
        # QTY Positive (+)
        # TOTAL_USD and TOTAL_GBP Negative (-)
        qty = qty
        total_usd = -total_usd
        total_gbp = -total_gbp

    elif txn_type == 'SELL':
        # QTY Negative (-)
        # TOTAL_USD and TOTAL_GBP Positive (+)
        qty = -qty
        total_usd = total_usd
        total_gbp = total_gbp

    return qty, total_usd, total_gbp

def main():
    logger.info("Starting Master Crypto Data Sheet generation...")

    # 1. Fetch FX
    fx_history = fetch_fx_history()
    if fx_history.empty:
        logger.error("FX history is empty. Aborting.")
        return

    logger.info(f"FX History fetched. Range: {fx_history.index.min().date()} to {fx_history.index.max().date()}")

    # 2. Parse Data
    df_coinbase = parse_coinbase(COINBASE_FILE_PATH)
    df_binance = parse_binance(BINANCE_FILE_PATH)

    if df_coinbase.empty and df_binance.empty:
        logger.error("No data found from either source. Exiting.")
        return

    # 3. Merge
    master_df = pd.concat([df_coinbase, df_binance], ignore_index=True)
    logger.info(f"Combined data. Total rows: {len(master_df)}")

    # 4. Master Logic Loop
    processed_rows = []

    for idx, row in master_df.iterrows():
        # Clean numeric inputs first
        qty_dec = safe_decimal_conversion(row['QTY'])
        total_usd_dec = safe_decimal_conversion(row['TOTAL_USD'])

        # A. FX Rate
        rate_val = get_fx_rate(row['TIME_UTC'], fx_history)
        rate_dec = Decimal(str(rate_val))

        # B. GBP Conversion
        if rate_dec == 0:
            total_gbp_dec = Decimal(0)
        else:
            # TOTAL_GBP = TOTAL_USD / FX_RATE
            total_gbp_dec = total_usd_dec / rate_dec

        # Update row with preliminary values (as Decimals)
        row['QTY'] = qty_dec
        row['TOTAL_USD'] = total_usd_dec
        row['FX_RATE'] = rate_dec
        row['TOTAL_GBP'] = total_gbp_dec

        # C. Signage Enforcement
        qty, total_usd, total_gbp = apply_signage_rules(row)

        row['QTY'] = qty
        row['TOTAL_USD'] = total_usd
        row['TOTAL_GBP'] = total_gbp

        processed_rows.append(row)

    final_df = pd.DataFrame(processed_rows)

    # 5. Final Formatting
    cols = ['EXCHANGE', 'TIME_UTC', 'TXN_TYPE', 'COIN', 'QTY', 'TOTAL_USD', 'FX_RATE', 'TOTAL_GBP']
    final_df = final_df[cols]

    # Sort
    final_df = final_df.sort_values(by='TIME_UTC', ascending=True)

    # Save
    final_df.to_csv(OUTPUT_FILE_NAME, index=False)

    # Summary
    n_coinbase = len(final_df[final_df['EXCHANGE'] == 'Coinbase'])
    n_binance = len(final_df[final_df['EXCHANGE'] == 'Binance'])

    print(f"\nSUCCESS. Total Rows: {len(final_df)}. Coinbase: {n_coinbase}, Binance: {n_binance}. File saved to {OUTPUT_FILE_NAME}.")

if __name__ == "__main__":
    main()
