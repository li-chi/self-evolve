#!/usr/bin/env python3
"""Oracle for stock-build-position.

The grader recomputes everything from LIVE yfinance data (latest daily open
prices + HKDUSD=X / CNYUSD=X open rates), so there is no static groundtruth.
This script performs the task for real: it fetches the same live prices the
grader will fetch, sizes a $1M portfolio 4:3:3 across US/HK/CN, and fills
/app/stock.xlsx. Because both sides read the same daily-open bar, the numbers
match at grading time (well within the grader's 3% tolerances).
"""
import sys
import pandas as pd
import yfinance as yf

TOTAL_USD = 1_000_000
REGION_BUDGET = {"us": 0.4 * TOTAL_USD, "hk": 0.3 * TOTAL_USD, "cn": 0.3 * TOTAL_USD}

STOCKS = {
    "us": {
        "Microsoft": "MSFT",
        "Apple": "AAPL",
        "NVIDIA": "NVDA",
        "AMD": "AMD",
        "Google": "GOOGL",
        "Meta": "META",
    },
    "hk": {
        "Meituan": "3690.HK",
        "Tencent": "0700.HK",
        "XIAOMI": "1810.HK",
        "Alibaba": "9988.HK",
    },
    "cn": {
        "Moutai": "600519.SS",
        "Ping An Insurance": "601318.SS",
        "BYD": "002594.SZ",
        "CATL": "300750.SZ",
        "WuXi AppTec": "603259.SS",
    },
}


def latest_open(ticker: str) -> float:
    hist = yf.Ticker(ticker).history(period="1d")
    if hist.empty:
        raise RuntimeError(f"no data for {ticker}")
    return float(hist["Open"].iloc[-1])


def to_usd(local_value: float, rate: float) -> float:
    # Mirror the grader's rate-direction handling.
    return local_value * rate if rate < 1 else local_value / rate


def main() -> int:
    prices = {}
    for region, stocks in STOCKS.items():
        for name, code in stocks.items():
            prices[name] = latest_open(code)
            print(f"open[{name} / {code}] = {prices[name]:.4f}")
    hkd_usd = latest_open("HKDUSD=X")
    cny_usd = latest_open("CNYUSD=X")
    print(f"HKDUSD=X open: {hkd_usd:.6f}, CNYUSD=X open: {cny_usd:.6f}")

    shares = {}

    # US: integer shares, per-stock target = 400k / 6
    per = REGION_BUDGET["us"] / len(STOCKS["us"])
    for name in STOCKS["us"]:
        shares[name] = max(1, round(per / prices[name]))

    # HK: integer shares, per-stock target = 300k / 4 (converted to HKD)
    per = REGION_BUDGET["hk"] / len(STOCKS["hk"])
    for name in STOCKS["hk"]:
        usd_price = to_usd(prices[name], hkd_usd)
        shares[name] = max(1, round(per / usd_price))

    # CN: multiples of 100 shares, per-stock target = 300k / 5
    per = REGION_BUDGET["cn"] / len(STOCKS["cn"])
    for name in STOCKS["cn"]:
        usd_lot = to_usd(prices[name], cny_usd) * 100
        shares[name] = max(1, round(per / usd_lot)) * 100

    def region_total(region: str) -> float:
        total = 0.0
        for name in STOCKS[region]:
            local = shares[name] * prices[name]
            if region == "hk":
                total += to_usd(local, hkd_usd)
            elif region == "cn":
                total += to_usd(local, cny_usd)
            else:
                total += local
        return total

    # Fine-tune the CN leg (coarsest granularity: 100-share lots of Moutai are
    # ~USD 20k) using lots of the cheapest CN stock.
    cheapest_cn = min(STOCKS["cn"], key=lambda n: prices[n])
    lot_usd = to_usd(prices[cheapest_cn] * 100, cny_usd)
    diff = REGION_BUDGET["cn"] - region_total("cn")
    adjust_lots = round(diff / lot_usd)
    if adjust_lots:
        new = shares[cheapest_cn] + 100 * adjust_lots
        if new >= 100:
            shares[cheapest_cn] = new
    print(f"CN adjustment: {adjust_lots} lots of {cheapest_cn}")

    totals = {r: region_total(r) for r in STOCKS}
    grand = sum(totals.values())
    print({r: round(v, 2) for r, v in totals.items()}, "grand:", round(grand, 2))

    # Self-check with the grader's own criteria before writing.
    assert abs(grand - TOTAL_USD) <= TOTAL_USD * 0.02, f"total off: {grand}"
    for region, target in (("us", 0.4), ("hk", 0.3), ("cn", 0.3)):
        ratio = totals[region] / grand
        print(f"{region} ratio: {ratio:.4f} (target {target})")
        assert abs(ratio - target) <= 0.02, f"{region} ratio off: {ratio}"

    name_to_code = {n: c for stocks in STOCKS.values() for n, c in stocks.items()}
    df = pd.read_excel("/app/stock.xlsx")
    df["Stock_code"] = df["Stock_name"].map(name_to_code)
    df["Initial_position_size"] = df["Stock_name"].map(
        {n: int(v) for n, v in shares.items()}
    )
    df.to_excel("/app/stock.xlsx", index=False)
    print("stock.xlsx written:")
    print(df.to_string())
    return 0


if __name__ == "__main__":
    sys.exit(main())
