# wikipedia-mock

Deterministic mock of [`wikipedia-mcp`](https://github.com/Rudra-ravi/wikipedia-mcp)
(atlas name `wikipedia`).

Serves the upstream tool surface from a seeded `state.json` — no live
Wikipedia API. Every tool is registered under its primary name **and** a
`wikipedia_`-prefixed alias, exactly like upstream.

## Tools
`get_article`, `get_summary`, `get_sections`, `get_links`,
`get_related_topics`, `extract_key_facts`, `summarize_article_for_query`,
`summarize_article_section` (+ `wikipedia_*` aliases) — all exact get-by-title,
the deterministic default surface. A `language` arg is accepted-and-ignored
(single-corpus mock).

`search_wikipedia` is **gated OFF by default** (query-phrasing-dependent
ranking). Set `WIKIPEDIA_MOCK_ENABLE_SEARCH=1` to expose it.

## State
`$WIKIPEDIA_MOCK_STATE_DIR/state.json`, seeded from `$WIKIPEDIA_MOCK_SEED_PATH`.
Build seeds with `synth/mock_seed/wikipedia.py`. Calls append to `state["calls"]`.
