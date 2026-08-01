"""Ground-truth computation for the yahoo-analysis oracle.

Vendored from the upstream grader (evaluation/main.py) with the grading
half removed: the reference answer for a live-data task has to be computed
from the same live source, with the same method, at solve time. Oracle-only
material — it never ships in the agent image.
"""

from argparse import ArgumentParser
from pathlib import Path
import json
import yfinance as yf
import pandas as pd
import re
from io import StringIO

def compare_rating(rating, stock_result):
    grade_to_direction = {
        # "Up"-predictions
        "Overweight":     "up",
        "Outperform":     "up",
        "Buy":            "up",
        "Upgrade":        "up",
        "Strong Buy":     "up",
        "Positive":       "up",
        "Accumulate":     "up",

        # "Flat"-predictions
        "Neutral":        "flat",
        "Hold":           "flat",
        "Sector Weight":  "flat",   # Follow sector movements, approximately "sideways"
        "Perform":        "flat",
        "Market Perform": "flat",
        "Equal Weight":   "flat",
        "Equal-Weight":   "flat",

        # "Down"-predictions
        "Sell":           "down",
        "Underperform":   "down",
        "Underweight":    "down",
        "Reduce":         "down",
    }
    direction = grade_to_direction.get(rating, None)

    if direction is None:
        print(f"Unknown rating: {rating}")
        return None
    
    results = {}
    start = stock_result['start']
    for horizon in ["4m", "5m", "6m"]:
        price = stock_result[horizon]
        if price is None:
            results[horizon] = None
            continue

        if direction == "up":
            results[horizon] = price > start
        elif direction == "flat":
            ret = (price - start) / start
            results[horizon] = abs(ret) <= 0.02
        elif direction == "down":
            results[horizon] = price < start
    return results

def compute_excess_return(stock_result, bench_result):
    """
    Calculate the excess returns of a stock relative to the benchmark index for 4, 5, and 6 months after the given date.
    Excess return = Stock return - Benchmark return

    Args:
        stock_result: dict
            Stock closing price results, in the same format as returned by get_stock_price.
        bench_result: dict
            Benchmark index closing price results, same format as get_stock_price.

    Returns:
        dict:
            {
                "4m": excess_return_4m,
                "5m": excess_return_5m,
                "6m": excess_return_6m
            }
    """
    excess_returns = {}
    start_stock = stock_result['start']
    start_bench = bench_result['start']
    for horizon in ["4m", "5m", "6m"]:
        stock_price = stock_result[horizon]
        bench_price = bench_result[horizon]
        if stock_price is None or bench_price is None:
            excess_returns[horizon] = None
        else:
            R_stock = (stock_price - start_stock) / start_stock
            R_bench = (bench_price - start_bench) / start_bench

            excess_returns[horizon] = (R_stock - R_bench) * 100  # Convert to percent

    return excess_returns
    
def get_stock_price(stock_hist: pd.DataFrame, bench_hist: pd.DataFrame, date, rating) -> dict:
    """
    Return stock and benchmark closing prices on the rating date and after
    4, 5, and 6 months. If a calendar date is not a trading day, use the
    first available trading day after it.
    """
    def _prepare(hist: pd.DataFrame):
        close = hist.sort_index()['Close'].dropna()
        return pd.DatetimeIndex(close.index), close

    stock_dates, stock_close = _prepare(stock_hist)
    bench_dates, bench_close = _prepare(bench_hist)

    # Treat Yahoo's daily 00:00 timestamps as calendar-day labels. Retaining
    # the intraday rating time would incorrectly skip or exclude that day's bar.
    rating_timestamp = pd.Timestamp(date)
    stock_tz = stock_dates.tz
    if stock_tz is not None and rating_timestamp.tzinfo is None:
        rating_timestamp = rating_timestamp.tz_localize(stock_tz)
    elif stock_tz is not None and rating_timestamp.tzinfo is not None:
        rating_timestamp = rating_timestamp.tz_convert(stock_tz)
    elif stock_tz is None and rating_timestamp.tzinfo is not None:
        rating_timestamp = rating_timestamp.tz_convert(None)
    rating_date = rating_timestamp.date()

    def _first_price_on_or_after(
        dates: pd.DatetimeIndex,
        closes: pd.Series,
        calendar_date,
    ) -> float | None:
        if len(dates) == 0:
            return None

        # Localize the same YYYY-MM-DD independently for each market index;
        # converting a midnight between timezones could shift the calendar day.
        target = pd.Timestamp(calendar_date)
        if dates.tz is not None:
            target = target.tz_localize(dates.tz)

        pos = dates.searchsorted(target, side='left')
        if pos >= len(dates):
            return None
        return float(closes.iloc[pos])

    def _get_prices(dates, closes):
        base_date = pd.Timestamp(rating_date)
        out = {'start': _first_price_on_or_after(dates, closes, rating_date)}
        for m in (4, 5, 6):
            target_date = (base_date + pd.DateOffset(months=m)).date()
            out[f'{m}m'] = _first_price_on_or_after(dates, closes, target_date)
        return out

    return {
        "stock":     _get_prices(stock_dates, stock_close),
        "benchmark": _get_prices(bench_dates, bench_close)
    }


def get_gt(ticker):
    stock = yf.Ticker(ticker)
    stock_hist = stock.history(period='2y')
    bench = yf.Ticker('^GSPC')  # S&P 500
    bench_hist = bench.history(period='2y')
    ratings = stock.upgrades_downgrades
    market_tz = pd.DatetimeIndex(stock_hist.index).tz
    rating_dates = pd.DatetimeIndex(ratings.index)
    if market_tz is not None and rating_dates.tz is None:
        rating_dates = rating_dates.tz_localize(market_tz)
    elif market_tz is not None and rating_dates.tz is not None:
        rating_dates = rating_dates.tz_convert(market_tz)
    elif market_tz is None and rating_dates.tz is not None:
        rating_dates = rating_dates.tz_convert(None)

    today = pd.Timestamp.now(tz=market_tz).normalize()
    two_years_ago = today - pd.DateOffset(years=2)
    normalized_rating_dates = rating_dates.normalize()
    recent_ratings = ratings[
        (normalized_rating_dates >= two_years_ago)
        & (normalized_rating_dates <= today)
    ]

    # Initialize statistics container
    results = {
        "4m": {"hit": 0, "excess": 0.0, "signals": 0, "fails": 0},
        "5m": {"hit": 0, "excess": 0.0, "signals": 0, "fails": 0},
        "6m": {"hit": 0, "excess": 0.0, "signals": 0, "fails": 0},
    }

    # Traverse each rating
    for dt, row in recent_ratings.iterrows():
        rating = row["ToGrade"]
        # Get prices for stock and benchmark
        info = get_stock_price(stock_hist, bench_hist, dt, rating)
        stock_res = info["stock"]
        bench_res = info["benchmark"]

        # If start price is missing, exclude whole signal
        if stock_res["start"] is None or bench_res["start"] is None:
            # print(f"Start price is missing for {dt}, {rating} is excluded for all horizons")
            for h in ("4m", "5m", "6m"):
                results[h]["fails"] += 1
            continue

        # Hit result by direction
        hit_map = compare_rating(rating, stock_res)

        # Excess return
        excess_map = compute_excess_return(stock_res, bench_res)

        # Aggregate results
        for h in ("4m", "5m", "6m"):
            # If future price is missing, treat as excluded
            if stock_res[h] is None or bench_res[h] is None:
                # print(f"Future price is missing for {dt}, {rating} is excluded for {h}")
                results[h]["fails"] += 1
                continue

            results[h]["signals"] += 1
            # Add hit if matched
            if hit_map[h]:
                results[h]["hit"] += 1
            # Accumulate excess return
            results[h]["excess"] += excess_map[h]

    # Calculate Hit Rate (%) and Avg Excess Return (%)
    summary = {}
    for h, stats in results.items():
        included_signals = stats["signals"]
        total_signals = included_signals + stats["fails"]
        hit_rate = (stats["hit"] / included_signals * 100) if included_signals > 0 else None
        avg_excess = (stats["excess"] / included_signals) if included_signals > 0 else None

        summary[h] = {
            "Hit Rate (%)": round(hit_rate, 2) if hit_rate is not None else None,
            "Avg Excess Return (%)": round(avg_excess, 2) if avg_excess is not None else None,
            # The guide defines #Signals as all ratings in the two-year window.
            "#Signals": total_signals,
            "Fails": stats["fails"],
        }

    return summary

