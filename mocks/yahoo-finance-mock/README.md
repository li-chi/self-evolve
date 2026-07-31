# yahoo-finance-mock

Deterministic mock of [`yahoo-finance-mcp`](https://github.com/Alex2Yang97/yahoo-finance-mcp)
(atlas name `yahoo-finance`; Toolathlon fork `lockon-n/yahoo-finance-mcp`).

Mirrors the upstream tool surface verbatim — the real server wraps `yfinance`
and every tool returns a JSON **string**; this mock returns the same shapes
from a seeded `state.json` so runs are deterministic and free (no live Yahoo
calls, no cassette replay).

## Tools
`get_historical_stock_prices`, `get_stock_info`, `get_yahoo_finance_news`,
`get_stock_actions`, `get_financial_statement`, `get_holder_info`,
`get_option_expiration_dates`, `get_option_chain`, `get_recommendations`.

Enum args (`financial_type`, `holder_type`, `option_type`,
`recommendation_type`) are validated against upstream's members and return the
same `"Error: ..."` string on a bad value.

## State
`$YAHOO_FINANCE_MOCK_STATE_DIR/state.json`, seeded from
`$YAHOO_FINANCE_MOCK_SEED_PATH`. Build seeds with
`synth/mock_seed/yahoo_finance.py`. Every call is appended to `state["calls"]`
for verifier consumption.
