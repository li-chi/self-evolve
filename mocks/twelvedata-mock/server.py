"""Twelve Data mock MCP server.

Mirrors the tool surface of `mcp-server-twelve-data` (the official
PyPI package the atlas registers as `twelvedata`, source:
https://pypi.org/project/mcp-server-twelve-data/0.2.5/). The real
server is a thin proxy over the Twelve Data REST API
(https://api.twelvedata.com); each tool maps 1:1 to a REST endpoint
and forwards a Pydantic-validated query-string. This mock returns
the same JSON shapes from a seeded JSON state file so RL training is
deterministic and cost-free (the real API is paid).

Implemented subset (PascalCase tool names matching upstream verbatim):

  Quotes/Prices  GetQuote, GetPrice, GetEod
  Time series    GetTimeSeries
  FX/Crypto      GetExchangeRate, GetCurrencyConversion
  Reference      GetSymbolSearch, GetEarliestTimestamp, GetMarketState,
                 GetStocks, GetForexPairs, GetCryptocurrencies,
                 GetExchanges
  Movers         GetMarketMovers

Indicator tools (GetTimeSeriesRsi/Macd/Sma/Ema/Bbands/...) and
fundamentals (GetStatistics, GetProfile, GetEarnings, GetDividends,
GetIncomeStatement, GetBalanceSheet, GetCashFlow, ...) are NOT shipped
in v1 — the upstream exposes ~120 such endpoints and none of the
current Tier A tasks exercise them. Add as task coverage grows.

Tool argument shape mirrors the upstream `<Tool>Request` Pydantic
model: e.g. `GetTimeSeries(symbol="AAPL", interval="1day",
outputsize=5)`. The `apikey` field is accepted-and-ignored. Responses
return the exact REST shapes (`meta` + `values` for time series; flat
`symbol`/`price`/... for quotes). Errors return the Twelve Data error
body:  `{"status":"error","code":404,"message":"..."}` — matching the
real API exactly, NOT an HTTPException.

State lives at `$TWELVEDATA_MOCK_STATE_DIR/state.json` (default
`~/.openclaw/twelvedata_mock/state.json`). Seedable from
`$TWELVEDATA_MOCK_SEED_PATH` on first start. Every call appends to
`state["calls"]` for verifier consumption.
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


# Twelve Data interval set, copied from upstream request models.
_INTERVALS = {"1min", "5min", "15min", "30min", "45min",
              "1h", "2h", "4h", "5h", "1day", "1week", "1month"}


def _state_path() -> str:
    state_dir = os.environ.get(
        "TWELVEDATA_MOCK_STATE_DIR",
        os.path.expanduser("~/.openclaw/twelvedata_mock"),
    )
    os.makedirs(state_dir, exist_ok=True)
    return os.path.join(state_dir, "state.json")


def _now_unix() -> int:
    return int(time.time())


def _now_iso() -> str:
    return (datetime.datetime.now(datetime.timezone.utc)
            .isoformat(timespec="seconds").replace("+00:00", "Z"))


def _empty_state() -> dict:
    return {
        "symbols": {},
        "exchanges": [],
        "forex_pairs": [],
        "cryptocurrencies": [],
        "movers": {"gainers": [], "losers": []},
        "calls": [],
    }


def _load_state() -> dict:
    path = _state_path()
    if not os.path.exists(path):
        seed = os.environ.get("TWELVEDATA_MOCK_SEED_PATH")
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
    lock_path = _state_path() + ".lock"
    fd = open(lock_path, "w")
    try:
        fcntl.flock(fd.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
        finally:
            fd.close()


def _record(state: dict, op: str, **kwargs: Any) -> None:
    entry = {"op": op, "ts": _now_iso()}
    entry.update(kwargs)
    state["calls"].append(entry)


def _err(code: int, message: str) -> dict:
    """Twelve Data REST error body — verbatim shape from
    response_models.py error branch in upstream server.py."""
    return {"status": "error", "code": code, "message": message,
            "meta": {}}


def _resolve_symbol(state: dict, symbol: str | None,
                    exchange: str | None = None,
                    mic_code: str | None = None) -> dict | None:
    """Look up a seeded symbol entry. Match is case-insensitive on the
    symbol ticker; optional `exchange`/`mic_code` filters narrow when
    multiple seeded entries share a ticker."""
    if not symbol:
        return None
    target = symbol.upper().strip()
    syms = state.get("symbols", {})
    if target in syms:
        entry = syms[target]
        if exchange and (entry.get("exchange") or "").upper() != exchange.upper():
            pass
        elif mic_code and (entry.get("mic_code") or "").upper() != mic_code.upper():
            pass
        else:
            return entry
    for key, entry in syms.items():
        if key.upper() != target:
            continue
        if exchange and (entry.get("exchange") or "").upper() != exchange.upper():
            continue
        if mic_code and (entry.get("mic_code") or "").upper() != mic_code.upper():
            continue
        return entry
    return None


def _trim_series(values: list, outputsize: int, order: str) -> list:
    """Apply Twelve Data's `outputsize` (1..5000) + `order` semantics."""
    try:
        n = int(outputsize)
    except (TypeError, ValueError):
        n = 10
    n = max(1, min(n, 5000))
    out = list(values)
    out.sort(key=lambda r: r.get("datetime", ""), reverse=True)
    out = out[:n]
    if (order or "desc").lower() == "asc":
        out.reverse()
    return out


def _fx_pair(symbol: str) -> tuple[str, str] | None:
    """Split `EUR/USD`-style pair; return (base, quote) or None."""
    if not symbol or "/" not in symbol:
        return None
    a, b = symbol.split("/", 1)
    a, b = a.strip().upper(), b.strip().upper()
    if not a or not b:
        return None
    return a, b


mcp = FastMCP("twelvedata-mock")


# ---------------------------------------------------------------------------
# Quote / Price / EOD
# ---------------------------------------------------------------------------

@mcp.tool(name="GetQuote",
          description="Quote endpoint is an efficient method to retrieve "
          "the latest quote of the selected instrument.")
def get_quote(symbol: str,
              figi: str | None = None,
              isin: str | None = None,
              cusip: str | None = None,
              interval: str = "1day",
              exchange: str | None = None,
              mic_code: str | None = None,
              country: str | None = None,
              volume_time_period: int = 9,
              type: str | None = None,
              format: str = "JSON",
              delimiter: str = ";",
              prepost: bool = False,
              eod: bool = False,
              rolling_period: int = 24,
              dp: int = 5,
              timezone: str = "Exchange",
              outputsize: int = 10,
              apikey: str = "demo") -> dict:
    """Twelve Data REST: GET /quote — latest quote for an instrument.
    Returns the flat quote shape (symbol, name, exchange, mic_code,
    currency, datetime, timestamp, open, high, low, close, volume,
    previous_close, change, percent_change, is_market_open,
    fifty_two_week, ...). See upstream GetQuote200Response."""
    with _lock():
        s = _load_state()
        entry = _resolve_symbol(s, symbol, exchange, mic_code)
        _record(s, "get_quote", symbol=symbol,
                result="ok" if entry else "not_found")
        _save_state(s)
        if not entry:
            return _err(404, f"**symbol** {symbol} not found. "
                             "Please check the symbol parameter")
        q = dict(entry.get("quote") or {})
        out = {
            "symbol": entry.get("symbol", symbol),
            "name": entry.get("name"),
            "exchange": entry.get("exchange"),
            "mic_code": entry.get("mic_code"),
            "currency": entry.get("currency"),
            "datetime": q.get("datetime"),
            "timestamp": q.get("timestamp"),
            "last_quote_at": q.get("last_quote_at") or q.get("timestamp"),
            "open": q.get("open"),
            "high": q.get("high"),
            "low": q.get("low"),
            "close": q.get("close") or q.get("price"),
            "volume": q.get("volume"),
            "previous_close": q.get("previous_close"),
            "change": q.get("change"),
            "percent_change": q.get("percent_change"),
            "average_volume": q.get("average_volume"),
            "is_market_open": q.get("is_market_open", True),
            "fifty_two_week": q.get("fifty_two_week"),
        }
        return {k: v for k, v in out.items() if v is not None}


@mcp.tool(name="GetPrice",
          description="This endpoint is a lightweight method that allows "
          "retrieving only the real-time price of the selected instrument.")
def get_price(symbol: str,
              figi: str | None = None,
              isin: str | None = None,
              cusip: str | None = None,
              exchange: str | None = None,
              mic_code: str | None = None,
              country: str | None = None,
              type: str | None = None,
              format: str = "JSON",
              delimiter: str = ";",
              prepost: bool = False,
              dp: int = 5,
              outputsize: int = 10,
              apikey: str = "demo") -> dict:
    """Twelve Data REST: GET /price — `{"price":"<latest>"}`."""
    with _lock():
        s = _load_state()
        entry = _resolve_symbol(s, symbol, exchange, mic_code)
        _record(s, "get_price", symbol=symbol,
                result="ok" if entry else "not_found")
        _save_state(s)
        if not entry:
            return _err(404, f"**symbol** {symbol} not found. "
                             "Please check the symbol parameter")
        q = entry.get("quote") or {}
        return {"price": q.get("price") or q.get("close") or "0.00000"}


@mcp.tool(name="GetEod",
          description="This endpoint returns the latest End of Day (EOD) "
          "price of an instrument.")
def get_eod(symbol: str,
            figi: str | None = None,
            isin: str | None = None,
            cusip: str | None = None,
            exchange: str | None = None,
            mic_code: str | None = None,
            country: str | None = None,
            type: str | None = None,
            date: str | None = None,
            prepost: bool = False,
            dp: int = 5,
            outputsize: int = 10,
            apikey: str = "demo") -> dict:
    """Twelve Data REST: GET /eod — latest end-of-day close. Returns
    {symbol, exchange, mic_code, currency, datetime, close}."""
    with _lock():
        s = _load_state()
        entry = _resolve_symbol(s, symbol, exchange, mic_code)
        _record(s, "get_eod", symbol=symbol, date=date,
                result="ok" if entry else "not_found")
        _save_state(s)
        if not entry:
            return _err(404, f"**symbol** {symbol} not found. "
                             "Please check the symbol parameter")
        # If a date is supplied and we have a series, find the matching bar.
        close = None
        dt = None
        if date:
            series = (entry.get("series") or {}).get("1day", [])
            for bar in series:
                if (bar.get("datetime") or "").startswith(date):
                    close = bar.get("close")
                    dt = bar.get("datetime")
                    break
        if close is None:
            series = (entry.get("series") or {}).get("1day", [])
            if series:
                latest = max(series, key=lambda b: b.get("datetime", ""))
                close = latest.get("close")
                dt = latest.get("datetime")
            else:
                q = entry.get("quote") or {}
                close = q.get("close") or q.get("price")
                dt = q.get("datetime")
        return {
            "symbol": entry.get("symbol", symbol),
            "exchange": entry.get("exchange"),
            "mic_code": entry.get("mic_code"),
            "currency": entry.get("currency"),
            "datetime": dt,
            "close": close,
        }


# ---------------------------------------------------------------------------
# Time series
# ---------------------------------------------------------------------------

@mcp.tool(name="GetTimeSeries",
          description="This API call returns meta and time series for the "
          "requested instrument. Metaobject consists of general information "
          "about the requested symbol. Time series is the array of objects "
          "ordered by time descending with Open, High, Low, Close prices. "
          "Non-currency instruments also include volume information.")
def get_time_series(symbol: str,
                    isin: str | None = None,
                    figi: str | None = None,
                    cusip: str | None = None,
                    interval: str = "1day",
                    outputsize: int = 10,
                    exchange: str | None = None,
                    mic_code: str | None = None,
                    country: str | None = None,
                    type: str | None = None,
                    timezone: str = "Exchange",
                    start_date: str | None = None,
                    end_date: str | None = None,
                    date: str | None = None,
                    order: str = "desc",
                    prepost: bool = False,
                    format: str = "JSON",
                    delimiter: str = ";",
                    dp: int = -1,
                    previous_close: bool = False,
                    adjust: str = "splits",
                    apikey: str = "demo") -> dict:
    """Twelve Data REST: GET /time_series — meta + values OHLCV.

    Returns {meta:{symbol, interval, currency, exchange_timezone,
    exchange, mic_code, type}, values:[{datetime, open, high, low,
    close, volume}, ...], status:"ok"}. `start_date`/`end_date`/`date`
    are honored as substring/range filters on the seeded bars'
    `datetime` field; `outputsize` is then applied (1..5000)."""
    with _lock():
        s = _load_state()
        if interval not in _INTERVALS:
            _record(s, "get_time_series", symbol=symbol,
                    interval=interval, result="bad_interval")
            _save_state(s)
            return _err(400, f"**interval** {interval} is not supported")
        entry = _resolve_symbol(s, symbol, exchange, mic_code)
        if not entry:
            _record(s, "get_time_series", symbol=symbol,
                    interval=interval, result="not_found")
            _save_state(s)
            return _err(404, f"**symbol** {symbol} not found. "
                             "Please check the symbol parameter")
        series = (entry.get("series") or {}).get(interval, [])
        rows = list(series)
        if date:
            rows = [r for r in rows
                    if (r.get("datetime") or "").startswith(date)]
        if start_date:
            rows = [r for r in rows
                    if (r.get("datetime") or "") >= start_date]
        if end_date:
            rows = [r for r in rows
                    if (r.get("datetime") or "") <= end_date]
        rows = _trim_series(rows, outputsize, order)
        meta = {
            "symbol": entry.get("symbol", symbol),
            "interval": interval,
            "currency": entry.get("currency"),
            "exchange_timezone": entry.get("exchange_timezone",
                                           "America/New_York"),
            "exchange": entry.get("exchange"),
            "mic_code": entry.get("mic_code"),
            "type": entry.get("type"),
        }
        _record(s, "get_time_series", symbol=symbol, interval=interval,
                count=len(rows))
        _save_state(s)
        return {"meta": meta, "values": rows, "status": "ok"}


# ---------------------------------------------------------------------------
# FX / Crypto rate
# ---------------------------------------------------------------------------

@mcp.tool(name="GetExchangeRate",
          description="This API call returns real-time exchange rate for "
          "currency pair. Works with forex and cryptocurrency.")
def get_exchange_rate(symbol: str,
                      date: str | None = None,
                      format: str = "JSON",
                      delimiter: str = ";",
                      dp: int = 5,
                      timezone: str | None = None,
                      outputsize: int = 10,
                      apikey: str = "demo") -> dict:
    """Twelve Data REST: GET /exchange_rate — `{symbol, rate, timestamp}`.
    `symbol` is a slash-delimited pair, e.g. `EUR/USD`, `BTC/USD`. The
    mock looks up the pair as a seeded symbol entry; if the pair isn't
    seeded but both legs are present as quote entries, an implied rate
    is returned (close_base_in_usd / close_quote_in_usd is NOT computed
    — only direct USD-quoted pairs interpolate)."""
    with _lock():
        s = _load_state()
        pair = _fx_pair(symbol)
        if not pair:
            _record(s, "get_exchange_rate", symbol=symbol,
                    result="bad_pair")
            _save_state(s)
            return _err(400, "**symbol** must be a currency pair "
                             "in the form `BASE/QUOTE`")
        entry = _resolve_symbol(s, symbol)
        rate = None
        if entry:
            q = entry.get("quote") or {}
            raw = q.get("price") or q.get("close") or q.get("rate")
            try:
                rate = float(raw) if raw is not None else None
            except (TypeError, ValueError):
                rate = None
        _record(s, "get_exchange_rate", symbol=symbol,
                result="ok" if rate is not None else "not_found")
        _save_state(s)
        if rate is None:
            return _err(404, f"**symbol** {symbol} not found. "
                             "Please check the symbol parameter")
        return {"symbol": symbol, "rate": rate, "timestamp": _now_unix()}


@mcp.tool(name="GetCurrencyConversion",
          description="This API call returns real-time exchange rate and "
          "converted amount for currency pair. Works with forex and "
          "cryptocurrency.")
def get_currency_conversion(symbol: str,
                            amount: float,
                            date: str | None = None,
                            format: str = "JSON",
                            delimiter: str = ";",
                            dp: int = 5,
                            timezone: str | None = None,
                            outputsize: int = 10,
                            apikey: str = "demo") -> dict:
    """Twelve Data REST: GET /currency_conversion —
    `{symbol, rate, amount, timestamp}`. Built on top of
    GetExchangeRate; multiplies the rate by `amount`."""
    rate_resp = get_exchange_rate(symbol=symbol, date=date, format=format,
                                  delimiter=delimiter, dp=dp,
                                  timezone=timezone, outputsize=outputsize,
                                  apikey=apikey)
    if isinstance(rate_resp, dict) and rate_resp.get("status") == "error":
        return rate_resp
    rate = rate_resp.get("rate", 0.0)
    try:
        amt = float(amount)
    except (TypeError, ValueError):
        amt = 0.0
    return {"symbol": symbol, "rate": rate,
            "amount": round(rate * amt, max(0, int(dp or 5))),
            "timestamp": rate_resp.get("timestamp", _now_unix())}


# ---------------------------------------------------------------------------
# Reference data
# ---------------------------------------------------------------------------

@mcp.tool(name="GetSymbolSearch",
          description="This method helps to find the best matching symbol. "
          "It can be used as the base for custom lookups. The response is "
          "returned in descending order, with the most relevant instrument "
          "at the beginning.")
def get_symbol_search(symbol: str,
                      outputsize: int = 10,
                      show_plan: bool = False,
                      apikey: str = "demo") -> dict:
    """Twelve Data REST: GET /symbol_search — `{data:[{symbol,
    instrument_name, exchange, mic_code, exchange_timezone,
    instrument_type, country, currency}, ...], status:"ok"}`.

    Match is a case-insensitive prefix match on the seeded ticker,
    then a substring match on the name. Up to `outputsize` results
    (cap 120 per upstream)."""
    with _lock():
        s = _load_state()
        q = (symbol or "").strip().upper()
        results: list[dict] = []
        seen: set[str] = set()
        for key, entry in s.get("symbols", {}).items():
            if not q:
                break
            sym = (entry.get("symbol") or key).upper()
            name = (entry.get("name") or "").upper()
            score = 0
            if sym == q:
                score = 3
            elif sym.startswith(q):
                score = 2
            elif q in name:
                score = 1
            elif q in sym:
                score = 1
            if score == 0:
                continue
            uid = f"{sym}:{entry.get('mic_code') or ''}"
            if uid in seen:
                continue
            seen.add(uid)
            results.append((score, {
                "symbol": entry.get("symbol", key),
                "instrument_name": entry.get("name"),
                "exchange": entry.get("exchange"),
                "mic_code": entry.get("mic_code"),
                "exchange_timezone": entry.get("exchange_timezone",
                                               "America/New_York"),
                "instrument_type": entry.get("type"),
                "country": entry.get("country"),
                "currency": entry.get("currency"),
            }))
        results.sort(key=lambda r: r[0], reverse=True)
        size = max(1, min(int(outputsize or 10), 120))
        data = [r[1] for r in results[:size]]
        _record(s, "get_symbol_search", symbol=symbol, count=len(data))
        _save_state(s)
        return {"data": data, "status": "ok"}


@mcp.tool(name="GetEarliestTimestamp",
          description="This method returns the first available DateTime "
          "for a given instrument at the specific interval.")
def get_earliest_timestamp(symbol: str,
                           figi: str | None = None,
                           isin: str | None = None,
                           cusip: str | None = None,
                           interval: str = "1day",
                           exchange: str | None = None,
                           mic_code: str | None = None,
                           timezone: str = "Exchange",
                           outputsize: int = 10,
                           apikey: str = "demo") -> dict:
    """Twelve Data REST: GET /earliest_timestamp —
    `{unix_time, datetime}` of the earliest seeded bar at the
    requested interval."""
    with _lock():
        s = _load_state()
        entry = _resolve_symbol(s, symbol, exchange, mic_code)
        if not entry:
            _record(s, "get_earliest_timestamp", symbol=symbol,
                    result="not_found")
            _save_state(s)
            return _err(404, f"**symbol** {symbol} not found. "
                             "Please check the symbol parameter")
        series = (entry.get("series") or {}).get(interval, [])
        if not series:
            _record(s, "get_earliest_timestamp", symbol=symbol,
                    interval=interval, result="no_series")
            _save_state(s)
            return _err(404, f"No data found for **symbol** {symbol} "
                             f"at **interval** {interval}")
        earliest = min(series, key=lambda b: b.get("datetime", ""))
        dt = earliest.get("datetime")
        try:
            ts = int(datetime.datetime.fromisoformat(
                dt.replace("Z", "+00:00")).timestamp())
        except (TypeError, ValueError, AttributeError):
            ts = 0
        _record(s, "get_earliest_timestamp", symbol=symbol,
                interval=interval, datetime=dt)
        _save_state(s)
        return {"unix_time": ts, "datetime": dt}


@mcp.tool(name="GetMarketState",
          description="Check the state of all available exchanges, time to "
          "open, and time to close. Returns all available stock exchanges "
          "by default.")
def get_market_state(exchange: str | None = None,
                     code: str | None = None,
                     country: str | None = None,
                     outputsize: int = 10,
                     apikey: str = "demo") -> list:
    """Twelve Data REST: GET /market_state — list of
    `{name, code, country, is_market_open, time_after_open,
    time_to_open, time_to_close}` for seeded exchanges."""
    with _lock():
        s = _load_state()
        rows = list(s.get("exchanges", []))
        if exchange:
            rows = [r for r in rows
                    if (r.get("name") or "").lower() == exchange.lower()]
        if code:
            rows = [r for r in rows
                    if (r.get("code") or "").upper() == code.upper()]
        if country:
            rows = [r for r in rows
                    if (r.get("country") or "").lower() == country.lower()]
        n = max(1, min(int(outputsize or 10), 5000))
        rows = rows[:n]
        _record(s, "get_market_state", exchange=exchange, code=code,
                count=len(rows))
        _save_state(s)
        return rows


@mcp.tool(name="GetStocks",
          description="This API call returns an array of symbols available "
          "at Twelve Data API. This list is updated daily.")
def get_stocks(symbol: str | None = None,
               figi: str | None = None,
               isin: str | None = None,
               cusip: str | None = None,
               exchange: str | None = None,
               mic_code: str | None = None,
               country: str | None = None,
               type: str | None = None,
               format: str = "JSON",
               delimiter: str = ";",
               show_plan: bool = False,
               include_delisted: bool = False,
               outputsize: int = 10,
               apikey: str = "demo") -> dict:
    """Twelve Data REST: GET /stocks — `{data:[StocksResponseItem],
    status:"ok"}`. Filters: symbol/exchange/mic_code/country/type
    (case-insensitive exact match on the seeded entries)."""
    with _lock():
        s = _load_state()
        data = []
        for key, e in s.get("symbols", {}).items():
            if e.get("type") and "Currency" in (e.get("type") or ""):
                continue
            if symbol and (e.get("symbol") or key).upper() != symbol.upper():
                continue
            if exchange and (e.get("exchange") or "").lower() != exchange.lower():
                continue
            if mic_code and (e.get("mic_code") or "").upper() != mic_code.upper():
                continue
            if country and (e.get("country") or "").lower() != country.lower():
                continue
            if type and (e.get("type") or "").lower() != type.lower():
                continue
            data.append({
                "symbol": e.get("symbol", key),
                "name": e.get("name"),
                "currency": e.get("currency"),
                "exchange": e.get("exchange"),
                "mic_code": e.get("mic_code"),
                "country": e.get("country"),
                "type": e.get("type"),
                "figi_code": e.get("figi_code"),
                "cfi_code": e.get("cfi_code"),
                "isin": e.get("isin"),
                "cusip": e.get("cusip"),
            })
        n = max(1, min(int(outputsize or 10), 5000))
        _record(s, "get_stocks", count=len(data[:n]))
        _save_state(s)
        return {"data": data[:n], "status": "ok"}


@mcp.tool(name="GetForexPairs",
          description="This API call returns an array of forex pairs "
          "available at Twelve Data API. This list is updated daily.")
def get_forex_pairs(symbol: str | None = None,
                    currency_base: str | None = None,
                    currency_quote: str | None = None,
                    format: str = "JSON",
                    delimiter: str = ";",
                    outputsize: int = 10,
                    apikey: str = "demo") -> dict:
    """Twelve Data REST: GET /forex_pairs — `{data:[{symbol,
    currency_group, currency_base, currency_quote}], status:"ok"}`."""
    with _lock():
        s = _load_state()
        items = list(s.get("forex_pairs", []))
        if symbol:
            items = [i for i in items
                     if (i.get("symbol") or "").upper() == symbol.upper()]
        if currency_base:
            items = [i for i in items
                     if (i.get("currency_base") or "").upper()
                     == currency_base.upper()]
        if currency_quote:
            items = [i for i in items
                     if (i.get("currency_quote") or "").upper()
                     == currency_quote.upper()]
        n = max(1, min(int(outputsize or 10), 5000))
        items = items[:n]
        _record(s, "get_forex_pairs", count=len(items))
        _save_state(s)
        return {"data": items, "status": "ok"}


@mcp.tool(name="GetCryptocurrencies",
          description="This API call returns an array of cryptocurrencies "
          "available at Twelve Data API. This list is updated daily.")
def get_cryptocurrencies(symbol: str | None = None,
                         exchange: str | None = None,
                         currency_base: str | None = None,
                         currency_quote: str | None = None,
                         format: str = "JSON",
                         delimiter: str = ";",
                         outputsize: int = 10,
                         apikey: str = "demo") -> dict:
    """Twelve Data REST: GET /cryptocurrencies — `{data:[{symbol,
    available_exchanges, currency_base, currency_quote}],
    status:"ok"}`."""
    with _lock():
        s = _load_state()
        items = list(s.get("cryptocurrencies", []))
        if symbol:
            items = [i for i in items
                     if (i.get("symbol") or "").upper() == symbol.upper()]
        if currency_base:
            items = [i for i in items
                     if (i.get("currency_base") or "").lower()
                     == currency_base.lower()]
        if currency_quote:
            items = [i for i in items
                     if (i.get("currency_quote") or "").lower()
                     == currency_quote.lower()]
        if exchange:
            items = [i for i in items
                     if exchange.lower() in
                     [x.lower() for x in (i.get("available_exchanges") or [])]]
        n = max(1, min(int(outputsize or 10), 5000))
        items = items[:n]
        _record(s, "get_cryptocurrencies", count=len(items))
        _save_state(s)
        return {"data": items, "status": "ok"}


@mcp.tool(name="GetExchanges",
          description="This API call returns an array of stock or ETF "
          "exchanges available at Twelve Data API. This list is updated daily.")
def get_exchanges(type: str | None = None,
                  name: str | None = None,
                  code: str | None = None,
                  country: str | None = None,
                  format: str = "JSON",
                  delimiter: str = ";",
                  show_plan: bool = False,
                  outputsize: int = 10,
                  apikey: str = "demo") -> dict:
    """Twelve Data REST: GET /exchanges — `{data:[{title, name, code,
    country, timezone}], status:"ok"}`. The mock derives this from
    seeded `state.exchanges` (same shape used by GetMarketState)."""
    with _lock():
        s = _load_state()
        items: list[dict] = []
        for e in s.get("exchanges", []):
            if name and (e.get("name") or "").lower() != name.lower():
                continue
            if code and (e.get("code") or "").upper() != code.upper():
                continue
            if country and (e.get("country") or "").lower() != country.lower():
                continue
            items.append({
                "title": e.get("title") or e.get("name"),
                "name": e.get("name"),
                "code": e.get("code"),
                "country": e.get("country"),
                "timezone": e.get("timezone"),
            })
        n = max(1, min(int(outputsize or 10), 5000))
        items = items[:n]
        _record(s, "get_exchanges", count=len(items))
        _save_state(s)
        return {"data": items, "status": "ok"}


# ---------------------------------------------------------------------------
# Market movers
# ---------------------------------------------------------------------------

@mcp.tool(name="GetMarketMovers",
          description="Get the list of the top gaining or losing stocks "
          "today. Top gainers are ordered by the highest rate of price "
          "increase since the previous day's close. Top losers are ordered "
          "by the highest percentage of price decrease since the last day.")
def get_market_movers(market: str,
                      direction: str = "gainers",
                      outputsize: int = 10,
                      country: str = "USA",
                      price_greater_than: str | None = None,
                      dp: str = "5",
                      apikey: str = "demo") -> dict:
    """Twelve Data REST: GET /market_movers/{market} — `{values:
    [MarketMoversResponseValue], status:"ok"}`. `direction` is
    `gainers` or `losers`; the mock returns the corresponding
    pre-seeded list (sorted by `percent_change` descending for
    gainers, ascending for losers)."""
    with _lock():
        s = _load_state()
        movers = s.get("movers") or {}
        bucket = "gainers" if direction != "losers" else "losers"
        rows = list(movers.get(bucket, []))
        if price_greater_than:
            try:
                lo = float(price_greater_than)
                rows = [r for r in rows if float(r.get("last", 0)) > lo]
            except (TypeError, ValueError):
                pass
        rows.sort(key=lambda r: r.get("percent_change", 0),
                  reverse=(bucket == "gainers"))
        n = max(1, min(int(outputsize or 10), 50))
        rows = rows[:n]
        _record(s, "get_market_movers", market=market, direction=direction,
                count=len(rows))
        _save_state(s)
        return {"values": rows, "status": "ok"}


# ---------------------------------------------------------------------------
# Debug helpers (mock-only, not part of upstream surface)
# ---------------------------------------------------------------------------

@mcp.tool(name="mock_debug_state",
          description="Mock-only: return the persisted state dict (symbols, "
          "exchanges, movers, call log). Not part of Twelve Data API.")
def mock_debug_state() -> dict:
    with _lock():
        return _load_state()


@mcp.tool(name="mock_debug_seed_symbol",
          description="Mock-only: insert/replace a seeded symbol entry. "
          "Used by per-task preprocessing to seed fixtures.")
def mock_debug_seed_symbol(entry: dict) -> dict:
    """`entry` must include `symbol` (string). Other fields (`name`,
    `exchange`, `mic_code`, `currency`, `type`, `country`, `quote`,
    `series`) follow the layout documented at the top of this file."""
    with _lock():
        s = _load_state()
        if not isinstance(entry, dict) or not entry.get("symbol"):
            return _err(400, "entry must be an object with a `symbol` field")
        key = entry["symbol"].upper()
        s.setdefault("symbols", {})[key] = entry
        _record(s, "debug_seed_symbol", symbol=key)
        _save_state(s)
        return entry


if __name__ == "__main__":
    mcp.run()
