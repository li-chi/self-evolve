# arxiv-mock

Deterministic mock of [`arxiv-mcp-server`](https://github.com/blazickjp/arxiv-mcp-server)
(atlas names `arxiv` / `arxiv_local`).

Serves the upstream tool surface from a seeded `state.json` — no live arXiv
API, no cassette. Search matches seeded papers by term (title+abstract),
category, and published-date range; `download_paper` marks a paper stored so
`read_paper` can return its markdown body (same download-then-read flow as
upstream).

## Tools
`download_paper(paper_id)`, `list_papers()`, `read_paper(paper_id)` — exact
get-by-arXiv-id, the deterministic default surface.

`search_papers(query, …)` is **gated OFF by default**: free-text search is a
query-phrasing-dependent ranking (token/Token/tokens) that isn't reconstructable
under recompute verification. Set `ARXIV_MOCK_ENABLE_SEARCH=1` to expose it
(only for corpora that pass a search-determinism gate).

## State
`$ARXIV_MOCK_STATE_DIR/state.json`, seeded from `$ARXIV_MOCK_SEED_PATH`. Build
seeds with `synth/mock_seed/arxiv.py`. Calls append to `state["calls"]`.
