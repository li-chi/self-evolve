# twelvedata-mock

Mock MCP server mirroring [`mcp-server-twelve-data`](https://pypi.org/project/mcp-server-twelve-data/)
v0.2.5 — the upstream the atlas registers as `twelvedata`. Twelve Data
is a paid market-data REST API (https://api.twelvedata.com); the
official MCP server is a thin Pydantic-validated proxy over it. This
mock returns the same JSON shapes from a seeded JSON state file so RL
training is deterministic and cost-free.

## Tool surface

Tool names are upstream-verbatim PascalCase. Argument shape mirrors
the upstream `<Tool>Request` Pydantic model (in particular the
`apikey` field is accepted-and-ignored).

| tool                     | REST endpoint                |
|--------------------------|------------------------------|
| `GetQuote`               | GET /quote                   |
| `GetPrice`               | GET /price                   |
| `GetEod`                 | GET /eod                     |
| `GetTimeSeries`          | GET /time_series             |
| `GetExchangeRate`        | GET /exchange_rate           |
| `GetCurrencyConversion`  | GET /currency_conversion     |
| `GetSymbolSearch`        | GET /symbol_search           |
| `GetEarliestTimestamp`   | GET /earliest_timestamp      |
| `GetMarketState`         | GET /market_state            |
| `GetStocks`              | GET /stocks                  |
| `GetForexPairs`          | GET /forex_pairs             |
| `GetCryptocurrencies`    | GET /cryptocurrencies        |
| `GetExchanges`           | GET /exchanges               |
| `GetMarketMovers`        | GET /market_movers/{market}  |

Plus two mock-only debug tools:

- `mock_debug_state` — return the full persisted state dict.
- `mock_debug_seed_symbol(entry)` — insert/replace a symbol entry.

## Skipped in v1

The upstream registers ~120 tools, dominated by **technical
indicators** (`GetTimeSeriesRsi`, `GetTimeSeriesMacd`,
`GetTimeSeriesSma`/`Ema`/`Bbands`/`Atr`/`Adx`/`Cci`/`Vwap`/...) and
**fundamentals** (`GetStatistics`, `GetProfile`, `GetEarnings`,
`GetDividends`, `GetSplits`, `GetIncomeStatement`, `GetCashFlow`,
`GetBalanceSheet`, `GetRecommendations`, `GetPriceTarget`,
`GetEarningsCalendar`, `GetIpoCalendar`, `GetSplitsCalendar`,
`GetDividendsCalendar`, `GetAnalystRatings*`, `GetEpsTrend`,
`GetEpsRevisions`, `GetRevenueEstimate`, `GetGrowthEstimates`,
`GetInsiderTransactions`, `GetInstitutionalHolders`, `GetFundHolders`,
`GetKeyExecutives`, `GetMarketCap`, `GetCrossListings`,
`GetTechnicalIndicators`, `GetApiUsage`, `GetLogo`, `GetExchangeSchedule`,
`GetTaxInfo`, `GetSourceSanctionedEntities`, mutual-fund world breakdowns,
ETF/bond/funds lists, `GetTimeSeriesCross`, `u_tool`, `doc_tool`, ...).

None of these are exercised by any current Tier A task. Add as task
coverage grows — the pattern is mechanical: take the upstream
`<Tool>Request` field list, resolve the seeded symbol, return a
canned dict matching `<Tool>200Response`.

## State

`$TWELVEDATA_MOCK_STATE_DIR/state.json` (default
`/workspace/output/end_state/twelvedata/state.json` inside the
container; `~/.openclaw/twelvedata_mock/state.json` outside).

```jsonc
{
  "symbols": {
    "AAPL": {
      "symbol":"AAPL","name":"Apple Inc","exchange":"NASDAQ",
      "mic_code":"XNGS","currency":"USD","type":"Common Stock",
      "country":"United States","exchange_timezone":"America/New_York",
      "quote": {
        "price":"189.84","close":"189.84","change":"1.23",
        "percent_change":"0.65","volume":"45123456",
        "previous_close":"188.61","open":"189.10","high":"190.20",
        "low":"188.55","datetime":"2025-05-19","timestamp":1747641600,
        "is_market_open":true,
        "fifty_two_week":{"low":"164.08","high":"237.49","range":"164.08 - 237.49"}
      },
      "series": {
        "1day":  [{"datetime":"2025-05-19","open":"...","high":"...","low":"...","close":"...","volume":"..."}, ...],
        "1min":  [...],
        "5min":  [...]
      },
      "figi_code":"BBG000B9XRY4","isin":"US0378331005","cusip":"037833100"
    }
  },
  "exchanges": [
    {"title":"NASDAQ","name":"NASDAQ","code":"XNGS",
     "country":"United States","timezone":"America/New_York",
     "is_market_open":true,"time_after_open":"02:39:03",
     "time_to_open":"00:00:00","time_to_close":"05:20:57"}
  ],
  "forex_pairs": [{"symbol":"EUR/USD","currency_group":"Major","currency_base":"EUR","currency_quote":"USD"}],
  "cryptocurrencies": [{"symbol":"BTC/USD","available_exchanges":["Coinbase","Binance"],"currency_base":"Bitcoin","currency_quote":"US Dollar"}],
  "movers": {"gainers":[MarketMoversResponseValue,...], "losers":[...]},
  "calls": [{"op":"get_quote","ts":"...","symbol":"AAPL","result":"ok"}]
}
```

The `calls` log is what the verifier consumes — every tool appends an
entry. File-locking via `fcntl.flock` makes concurrent calls safe;
per-rollout isolation should reset the state dir between rollouts.

Seed a starting state via `TWELVEDATA_MOCK_SEED_PATH` (loaded once
when `state.json` does not yet exist). FX/crypto pairs need a
`symbols["EUR/USD"]` entry with `quote.price` for `GetExchangeRate` /
`GetCurrencyConversion` to return a rate.

## Errors

Match the real Twelve Data REST error shape (returned as a tool
result, not raised), so a failed call still looks like a real HTTP
response body:

```json
{"status": "error", "code": 404, "message": "**symbol** NOSUCH not found. Please check the symbol parameter", "meta": {}}
```

## Run

```bash
# local
TWELVEDATA_MOCK_STATE_DIR=$PWD/state python server.py

# docker (per-task compose snippet)
services:
  twelvedata-mock:
    build:
      context: ../../mcp_servers/twelvedata-mock
      dockerfile: Dockerfile
    image: mcp-env/twelvedata-mock:0.1.0
    stdin_open: true
    tty: false
    environment:
      TWELVEDATA_MOCK_STATE_DIR: /workspace/output/end_state/twelvedata
      TWELVEDATA_MOCK_SEED_PATH: /workspace/input/twelvedata_seed.json
    volumes:
      - ${AGENT_WORKSPACE:-./workspace}:/workspace
```
