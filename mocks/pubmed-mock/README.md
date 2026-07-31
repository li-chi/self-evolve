# pubmed-mock

Deterministic mock of [`PubMed-MCP-Server`](https://github.com/JackKuo666/PubMed-MCP-Server)
(atlas name `pubmed`).

Serves the upstream tool surface from a seeded `state.json` — no NCBI
E-utilities calls, no cassette. Search matches seeded articles by term over
title/abstract/keywords; advanced search ANDs term/title/author/journal/date
filters; `download_pubmed_pdf` only succeeds for articles flagged
`pdf_available`.

## Tools
`get_pubmed_article_metadata(pmid)`, `download_pubmed_pdf(pmid)`,
`deep_paper_analysis(pmid)` — exact get-by-PMID, the deterministic default surface.

`search_pubmed_key_words` and `search_pubmed_advanced` are **gated OFF by
default** (query-phrasing-dependent ranking, not reconstructable under recompute
verification). Set `PUBMED_MOCK_ENABLE_SEARCH=1` to expose them.

## State
`$PUBMED_MOCK_STATE_DIR/state.json`, seeded from `$PUBMED_MOCK_SEED_PATH`. Build
seeds with `synth/mock_seed/pubmed.py`. Calls append to `state["calls"]`.
