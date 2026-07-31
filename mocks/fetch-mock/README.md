# fetch-mock

Deterministic mock of [`@tokenizin/mcp-npx-fetch`](https://github.com/tokenizin-agency/mcp-npx-fetch)
(atlas name `fetch`).

Serves a seeded "mini-web" corpus instead of hitting live URLs — this is how
web-fetch stays deterministic and verifiable (no cassette). A page seeded with
one representation derives the rest (text from HTML by tag-stripping; Markdown
from text).

## Tools
`fetch_html(url, headers)`, `fetch_json(url, headers)`, `fetch_txt(url, headers)`,
`fetch_markdown(url, headers)`. `headers` is accepted-and-ignored.

## State
`$FETCH_MOCK_STATE_DIR/state.json`, seeded from `$FETCH_MOCK_SEED_PATH`. Build
seeds with `synth/mock_seed/fetch.py`. Calls append to `state["calls"]`.
