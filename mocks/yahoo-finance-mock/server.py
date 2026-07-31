"""Yahoo Finance mock MCP server.

Mirrors the tool surface of `yahoo-finance-mcp`
(https://github.com/Alex2Yang97/yahoo-finance-mcp — the server the atlas
registers as `yahoo-finance`; the Toolathlon fork lockon-n/yahoo-finance-mcp
is identical in tool surface). The real server is a thin wrapper over the
`yfinance` package; each tool returns a JSON **string** (matching upstream,
whose tools are `async def ... -> str`). This mock returns the same shapes
from a seeded JSON state file so RL training is deterministic and cost-free.

Tool surface (verbatim names + signatures from upstream server.py):

  get_historical_stock_prices(ticker, period="1mo", interval="1d")
  get_stock_info(ticker)
  get_yahoo_finance_news(ticker)
  get_stock_actions(ticker)
  get_financial_statement(ticker, financial_type)
  get_holder_info(ticker, holder_type)
  get_option_expiration_dates(ticker)
  get_option_chain(ticker, expiration_date, option_type)
  get_recommendations(ticker, recommendation_type, months_back=12)

Enum values (upstream `FinancialType` / `HolderType` / `OptionType` /
`RecommendationType`) are validated verbatim; an unknown value returns the
same "Error: ..." string the real server produces.

State lives at `$YAHOO_FINANCE_MOCK_STATE_DIR/state.json` (default
`~/.openclaw/yahoo_finance_mock`). Seedable from `$YAHOO_FINANCE_MOCK_SEED_PATH`
on first start. Every call appends to `state["calls"]`. State shape is built
by `synth/mock_seed/yahoo_finance.py`.
"""

from __future__ import annotations

import contextlib
import datetime
import fcntl
import json
import os
import time
from typing import Any

from mcp.server.fastmcp import FastMCP

# Upstream enum members (yfinance-mcp/server.py), matched verbatim.
_FINANCIAL_TYPES = {
    "income_stmt", "quarterly_income_stmt",
    "balance_sheet", "quarterly_balance_sheet",
    "cashflow", "quarterly_cashflow",
}
_HOLDER_TYPES = {
    "major_holders", "institutional_holders", "mutualfund_holders",
    "insider_transactions", "insider_purchases", "insider_roster_holders",
}
_OPTION_TYPES = {"calls", "puts"}
_RECOMMENDATION_TYPES = {"recommendations", "upgrades_downgrades"}


def _state_path() -> str:
    d = os.environ.get("YAHOO_FINANCE_MOCK_STATE_DIR",
                       os.path.expanduser("~/.openclaw/yahoo_finance_mock"))
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "state.json")


def _now_iso() -> str:
    return (datetime.datetime.now(datetime.timezone.utc)
            .isoformat(timespec="seconds").replace("+00:00", "Z"))


def _empty_state() -> dict:
    return {"tickers": {}, "calls": []}


def _load_state() -> dict:
    path = _state_path()
    if not os.path.exists(path):
        seed = os.environ.get("YAHOO_FINANCE_MOCK_SEED_PATH")
        if seed and os.path.exists(seed):
            with open(seed, "r", encoding="utf-8") as f:
                return json.load(f)
        return _empty_state()
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_state(state: dict) -> None:
    path = _state_path()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, path)


@contextlib.contextmanager
def _lock():
    fd = open(_state_path() + ".lock", "w")
    try:
        fcntl.flock(fd.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
        finally:
            fd.close()


def _record(state: dict, op: str, **kw: Any) -> None:
    entry = {"op": op, "ts": _now_iso()}
    entry.update(kw)
    state["calls"].append(entry)


def _ticker(state: dict, ticker: str) -> dict | None:
    if not ticker:
        return None
    return state.get("tickers", {}).get(ticker.upper().strip())


mcp = FastMCP("yahoo-finance-mock")


@mcp.tool(
    name="get_historical_stock_prices",
    description="Get historical stock prices for a given ticker symbol from "
    "yahoo finance. Include the following information: Date, Open, High, Low, "
    "Close, Volume, Dividends, Stock Splits.")
def get_historical_stock_prices(ticker: str, period: str = "1mo",
                                interval: str = "1d") -> str:
    """yfinance Ticker.history(period, interval) — returns a JSON string of
    the OHLCV records list. Valid periods: 1d,5d,1mo,3mo,6mo,1y,2y,5y,10y,
    ytd,max; intervals: 1m,2m,5m,15m,30m,60m,90m,1h,1d,5d,1wk,1mo,3mo."""
    with _lock():
        s = _load_state()
        t = _ticker(s, ticker)
        _record(s, "get_historical_stock_prices", ticker=ticker,
                period=period, interval=interval,
                result="ok" if t else "not_found")
        _save_state(s)
    if not t:
        return f"Error: getting historical stock prices for {ticker}: No data found"
    hist = (t.get("history") or {})
    rows = hist.get(f"{period}:{interval}") or hist.get(interval) or hist.get("default") or []
    return json.dumps(rows)


@mcp.tool(
    name="get_stock_info",
    description="Get stock information for a given ticker symbol from yahoo "
    "finance. Include the following information: Stock Price & Trading Info, "
    "Company Information, Financial Metrics, Earnings & Revenue, Margins & "
    "Returns, Dividends, Balance Sheet, Ownership, Analyst Coverage, Risk "
    "Metrics, Other.")
def get_stock_info(ticker: str) -> str:
    """yfinance Ticker.info — returns a JSON string of the info dict."""
    with _lock():
        s = _load_state()
        t = _ticker(s, ticker)
        _record(s, "get_stock_info", ticker=ticker,
                result="ok" if t else "not_found")
        _save_state(s)
    if not t:
        return f"Error: getting stock information for {ticker}: No data found"
    return json.dumps(t.get("info") or {})


@mcp.tool(
    name="get_yahoo_finance_news",
    description="Get news for a given ticker symbol from yahoo finance.")
def get_yahoo_finance_news(ticker: str) -> str:
    """yfinance Ticker.news — returns newline-joined title/publisher/link
    blocks (upstream formats each article as text)."""
    with _lock():
        s = _load_state()
        t = _ticker(s, ticker)
        _record(s, "get_yahoo_finance_news", ticker=ticker,
                result="ok" if t else "not_found")
        _save_state(s)
    if not t:
        return f"Error: getting news for {ticker}: No data found"
    news = t.get("news") or []
    if not news:
        return f"No news found for company that searched with {ticker} ticker."
    out = []
    for a in news:
        out.append(
            f"Title: {a.get('title', '')}\n"
            f"Summary: {a.get('summary', '')}\n"
            f"Publisher: {a.get('publisher', '')}\n"
            f"Link: {a.get('link', '')}\n")
    return "\n".join(out)


@mcp.tool(
    name="get_stock_actions",
    description="Get stock dividends and stock splits for a given ticker "
    "symbol from yahoo finance.")
def get_stock_actions(ticker: str) -> str:
    """yfinance Ticker.actions — returns a JSON string of the actions records
    (Date, Dividends, Stock Splits)."""
    with _lock():
        s = _load_state()
        t = _ticker(s, ticker)
        _record(s, "get_stock_actions", ticker=ticker,
                result="ok" if t else "not_found")
        _save_state(s)
    if not t:
        return f"Error: getting stock actions for {ticker}: No data found"
    return json.dumps((t.get("actions") or []))


@mcp.tool(
    name="get_financial_statement",
    description="Get financial statement for a given ticker symbol from yahoo "
    "finance. You can choose from the following financial statement types: "
    "income_stmt, quarterly_income_stmt, balance_sheet, "
    "quarterly_balance_sheet, cashflow, quarterly_cashflow.")
def get_financial_statement(ticker: str, financial_type: str) -> str:
    """yfinance income_stmt/balance_sheet/cashflow (annual or quarterly) —
    returns a JSON string. Unknown `financial_type` errors verbatim."""
    with _lock():
        s = _load_state()
        t = _ticker(s, ticker)
        _record(s, "get_financial_statement", ticker=ticker,
                financial_type=financial_type,
                result="ok" if t else "not_found")
        _save_state(s)
    if financial_type not in _FINANCIAL_TYPES:
        return (f"Error: invalid financial type {financial_type}. "
                f"Please use one of the following: {', '.join(sorted(_FINANCIAL_TYPES))}.")
    if not t:
        return f"Error: getting financial statement for {ticker}: No data found"
    return json.dumps((t.get("financials") or {}).get(financial_type) or {})


@mcp.tool(
    name="get_holder_info",
    description="Get holder information for a given ticker symbol from yahoo "
    "finance. You can choose from the following holder types: major_holders, "
    "institutional_holders, mutualfund_holders, insider_transactions, "
    "insider_purchases, insider_roster_holders.")
def get_holder_info(ticker: str, holder_type: str) -> str:
    """yfinance major/institutional/mutualfund holders + insider data —
    returns a JSON string. Unknown `holder_type` errors verbatim."""
    with _lock():
        s = _load_state()
        t = _ticker(s, ticker)
        _record(s, "get_holder_info", ticker=ticker, holder_type=holder_type,
                result="ok" if t else "not_found")
        _save_state(s)
    if holder_type not in _HOLDER_TYPES:
        return (f"Error: invalid holder type {holder_type}. "
                f"Please use one of the following: {', '.join(sorted(_HOLDER_TYPES))}.")
    if not t:
        return f"Error: getting holder info for {ticker}: No data found"
    return json.dumps((t.get("holders") or {}).get(holder_type) or {})


@mcp.tool(
    name="get_option_expiration_dates",
    description="Fetch the available options expiration dates for a given "
    "ticker symbol.")
def get_option_expiration_dates(ticker: str) -> str:
    """yfinance Ticker.options — returns a JSON string list of expiration
    date strings (YYYY-MM-DD)."""
    with _lock():
        s = _load_state()
        t = _ticker(s, ticker)
        _record(s, "get_option_expiration_dates", ticker=ticker,
                result="ok" if t else "not_found")
        _save_state(s)
    if not t:
        return f"Error: getting option expiration dates for {ticker}: No data found"
    return json.dumps(list((t.get("options") or {}).get("dates") or []))


@mcp.tool(
    name="get_option_chain",
    description="Fetch the option chain for a given ticker symbol, expiration "
    "date, and option type.")
def get_option_chain(ticker: str, expiration_date: str,
                     option_type: str) -> str:
    """yfinance Ticker.option_chain(date).calls/.puts — returns a JSON string
    of the chain records. `option_type` is 'calls' or 'puts'."""
    with _lock():
        s = _load_state()
        t = _ticker(s, ticker)
        _record(s, "get_option_chain", ticker=ticker,
                expiration_date=expiration_date, option_type=option_type,
                result="ok" if t else "not_found")
        _save_state(s)
    if option_type not in _OPTION_TYPES:
        return "Error: Invalid option type. Please use 'calls' or 'puts'."
    if not t:
        return f"Error: getting option chain for {ticker}: No data found"
    opts = (t.get("options") or {})
    if expiration_date not in (opts.get("dates") or []):
        return (f"Error: No options available for {ticker} on "
                f"{expiration_date}. Available dates: {opts.get('dates') or []}")
    chain = (opts.get("chains") or {}).get(f"{expiration_date}:{option_type}") or []
    return json.dumps(chain)


@mcp.tool(
    name="get_recommendations",
    description="Get recommendations or upgrades/downgrades for a given ticker "
    "symbol from yahoo finance. You can also specify the number of months back "
    "to get upgrades/downgrades for, default is 12.")
def get_recommendations(ticker: str, recommendation_type: str,
                        months_back: int = 12) -> str:
    """yfinance Ticker.recommendations / .upgrades_downgrades — returns a JSON
    string. Unknown `recommendation_type` errors verbatim."""
    with _lock():
        s = _load_state()
        t = _ticker(s, ticker)
        _record(s, "get_recommendations", ticker=ticker,
                recommendation_type=recommendation_type,
                months_back=months_back, result="ok" if t else "not_found")
        _save_state(s)
    if recommendation_type not in _RECOMMENDATION_TYPES:
        return (f"Error: invalid recommendation type {recommendation_type}. "
                f"Please use one of the following: {', '.join(sorted(_RECOMMENDATION_TYPES))}.")
    if not t:
        return f"Error: getting recommendations for {ticker}: No data found"
    return json.dumps((t.get("recommendations") or {}).get(recommendation_type) or [])


@mcp.tool(name="mock_debug_state",
          description="Mock-only: return the persisted state dict.")
def mock_debug_state() -> dict:
    with _lock():
        return _load_state()


if __name__ == "__main__":
    mcp.run()
