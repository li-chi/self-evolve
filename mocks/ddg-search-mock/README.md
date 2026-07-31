# ddg-search-mock

Deterministic mock of [`duckduckgo-mcp-server`](https://github.com/nickclyde/duckduckgo-mcp-server)
(atlas name `ddg-search`).

Serves a seeded search index + "mini-web" page corpus — no live DuckDuckGo,
no cassette. `search` ranks seeded results by term overlap and returns the
upstream-style formatted string; `fetch_content` returns a seeded page's text,
sliced by `start_index`/`max_length` exactly like upstream.

## Tools
`fetch_content(url, start_index=0, max_length=8000, backend=None)` — exact
fetch-by-URL, the deterministic default surface.

`search(query, …)` is **gated OFF by default** (query-phrasing-dependent
ranking, not reconstructable under recompute verification). Set
`DDG_SEARCH_MOCK_ENABLE_SEARCH=1` to expose it.

## State
`$DDG_SEARCH_MOCK_STATE_DIR/state.json`, seeded from
`$DDG_SEARCH_MOCK_SEED_PATH`. Build seeds with `synth/mock_seed/ddg_search.py`.
Calls append to `state["calls"]`.
