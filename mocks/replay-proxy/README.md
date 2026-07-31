# replay-proxy

Generic **Tier-B** replay-proxy MCP server: deterministic replay of
pre-recorded tool responses, keyed by `(tool, canonical args hash)`.

Tier-B servers are real public APIs whose answers drift over time
(stock quotes, web search, browser snapshots, weather) and which
can't be hit live during RL training because they're
non-deterministic and rate-limited. We can't mock them with a
stateful backend either (the ground truth lives upstream). The fix:
record real responses once, replay them deterministically thereafter.

This package is **infrastructure**, not a per-server mock. One
process can expose multiple Tier-B servers simultaneously by
declaring them in a config file.

## Layout

```
replay-proxy/
├── server.py                       # replay MCP server (FastMCP, dynamic tools)
├── record_server.py                # recording MCP server (subprocess upstream)
├── replay/
│   ├── __init__.py
│   ├── cassette.py                 # canonicalization, hashing, JSONL io
│   └── cli.py                      # `replay list|lookup|validate|hash|record|
│                                   #         record-server|record-replay-trajectory`
├── cassettes/
│   └── yahoo-finance.jsonl         # example
├── config.example.json             # replay config: yahoo-finance with 2 tools
├── recorder.config.example.json    # recorder config: spawn yahoo-finance upstream
├── Dockerfile
├── pyproject.toml
└── README.md
```

## Cassette format

One JSON object per line:

```json
{"server":"yahoo-finance","tool":"get_stock_quote","args_hash":"sha256:…",
 "args":{"ticker":"AAPL"},"response":{…},"recorded_at":"2026-05-15T20:05:00.000Z"}
```

### Canonicalization

Before hashing, args are normalized so that semantically-identical
calls map to the same cassette entry:

| Transformation                 | Example                                  |
|--------------------------------|------------------------------------------|
| Sorted keys                    | `{"b":2,"a":1}` → `{"a":1,"b":2}`        |
| Stripped surrounding whitespace| `"AAPL "` → `"AAPL"`                     |
| Upper-case for known keys      | `"aapl"` → `"AAPL"` (ticker, symbol, …)  |
| `None` values dropped          | `{"a":1,"b":null}` → `{"a":1}`           |
| Recurses into nested dicts     |                                          |

`CASE_INSENSITIVE_KEYS` in `replay/cassette.py` controls the
uppercase set — add entries when a server's tools take
case-insensitive identifiers (currency codes, country codes, …).

The hash is `sha256:` + hex of
`json.dumps(canon, sort_keys=True, separators=(",",":"))`.

## Server config

Set `REPLAY_PROXY_CONFIG` to a JSON file:

```jsonc
{
  "servers": [
    {
      "name": "yahoo-finance",
      "cassette": "/workspace/cassettes/yahoo-finance.jsonl",
      "tools": [
        {
          "name": "get_stock_quote",
          "description": "…",
          "params": {
            "ticker": {"type": "string", "required": true}
          }
        },
        {
          "name": "get_history",
          "params": {
            "ticker": {"type": "string", "required": true},
            "period": {"type": "string", "default": "5d"}
          }
        }
      ]
    }
  ]
}
```

`params` entries accept either a type string (`"string"`,
`"number"`, `"integer"`, `"boolean"`, `"array"`, `"object"`) or a
dict with `type` / `required` / `default`. Each declared tool is
registered with FastMCP under its exact `name`, with a synthetic
signature matching `params`.

## Miss policy

`REPLAY_PROXY_MISS_POLICY` controls what happens on a cassette miss:

| Value         | Behaviour                                              |
|---------------|--------------------------------------------------------|
| `error` (def) | return `{"error":"cassette_miss", "args_hash":…, …}`   |
| `null`        | return `null`                                           |
| `passthrough` | reserved; currently equivalent to `error` but logs intent (a real upstream-call shim is follow-up work) |

Every miss is appended to `$REPLAY_PROXY_STATE_DIR/misses.jsonl`
regardless of policy, so an offline recorder can fill the gap on
the next pass. Every call (hit + miss) is appended to
`$REPLAY_PROXY_STATE_DIR/calls.jsonl` for audit.

## State paths

```
$REPLAY_PROXY_STATE_DIR/
├── misses.jsonl   # one line per cassette miss
├── calls.jsonl    # one line per tool invocation
├── .calls.lock    # fcntl lockfile
└── .misses.lock
```

Default outside the container: `~/.openclaw/replay_proxy/`.
Inside the container: `/workspace/output/end_state/replay_proxy/`.

## Run

```bash
# local smoke test
REPLAY_PROXY_CONFIG=$PWD/config.example.json \
REPLAY_PROXY_STATE_DIR=$PWD/_state \
python server.py
```

```yaml
# docker (per-task compose snippet)
services:
  replay-proxy:
    build:
      context: ../../mcp_servers/replay-proxy
      dockerfile: Dockerfile
    image: mcp-env/replay-proxy:0.1.0
    stdin_open: true
    tty: false
    environment:
      REPLAY_PROXY_CONFIG: /workspace/replay/yahoo-finance.config.json
      REPLAY_PROXY_STATE_DIR: /workspace/output/end_state/replay_proxy
      REPLAY_PROXY_MISS_POLICY: error
    volumes:
      - ${AGENT_WORKSPACE:-./workspace}:/workspace
```

## CLI

```bash
# list cassette contents
python -m replay.cli list cassettes/yahoo-finance.jsonl

# resolve a (tool, args) pair against a cassette
python -m replay.cli lookup cassettes/yahoo-finance.jsonl \
    get_stock_quote '{"ticker":"AAPL"}'

# schema + hash check
python -m replay.cli validate cassettes/yahoo-finance.jsonl

# print the canonical hash of an args dict
python -m replay.cli hash '{"ticker":"aapl"}'

# append one entry by hand
python -m replay.cli record \
    --cassette cassettes/yahoo-finance.jsonl \
    --server yahoo-finance --tool get_stock_quote \
    --args '{"ticker":"NVDA"}' \
    --response '{"symbol":"NVDA","regularMarketPrice":950.0}'
```

The CLI is also installed as a `replay` console script via
`pyproject.toml`.

## Debug tool

`mock_debug_state` (registered by the server itself) returns:

```jsonc
{
  "config":           { ... },
  "miss_policy":      "error",
  "state_dir":        "...",
  "cassettes":        [{"server":..., "path":..., "entries": N, "tools": [...]}],
  "registered_tools": [{"server":..., "tool":..., "params": {...}, "cassette": "..."}],
  "totals":           {"calls": N, "misses": N}
}
```

## Recording playbook

There are three supported ways to grow a cassette:

1. **Hand-record from real API output.** Hit the upstream API once
   (curl / SDK / live MCP server), save the JSON response, then:
   ```bash
   python -m replay.cli record \
       --cassette cassettes/<server>.jsonl \
       --server <server> --tool <tool> \
       --args '{...}' --response "$(cat response.json)"
   ```
   `args_hash` is computed from canonicalized args — you do not
   need to compute it by hand.

2. **Replay-then-fill loop.** Run the proxy with `MISS_POLICY=error`
   against a partial cassette. Each unrecorded call lands in
   `misses.jsonl`. Re-record the missing entries (manually or via a
   per-server recorder script that consumes `misses.jsonl`),
   restart the proxy, repeat until misses stay empty.

3. **Live recording-proxy** (preferred for bootstrapping). See
   "Recording cassettes" below.

## Recording cassettes

`record_server.py` is the in-process recorder: a thin MCP server
that sits between an agent and a real upstream MCP server,
forwarding every `tool/call` to the upstream and appending each
`(tool, args, response)` triple to a cassette JSONL on the way back.
Cassette entries are hashed with the *same* canonicalizer the
replay-proxy uses, so the recorded entries will hit on replay.

```
agent ──stdio──► record_server.py ──stdio──► real upstream MCP server
                        │
                        ▼
                cassettes/<server>.jsonl
```

### Playbook

1. **Spawn the upstream once you have valid credentials.** Get the
   real upstream MCP server running locally (or runnable on demand
   by a launcher like `uv` / `npx`). Confirm it lists its tools,
   responds to a hand call, and has the API keys it needs. Do not
   yet connect your agent.

2. **Write a recorder config** based on
   `recorder.config.example.json`:
   ```jsonc
   {
     "server":   "yahoo-finance",
     "cassette": "/workspace/cassettes/yahoo-finance.jsonl",
     "upstream": {
       "command": "uv",
       "args":    ["run", "/path/to/upstream/server.py"],
       "env":     {"YAHOO_API_KEY": "..."},
       "cwd":     "/path/to/upstream"
     },
     "tool_allowlist": null,
     "tool_blocklist": []
   }
   ```
   `tool_allowlist` / `tool_blocklist` are optional. The recorder
   re-registers every selected upstream tool on its own stdio with
   the same name and JSON-schema-derived parameter list, so the
   agent sees the upstream surface verbatim.

3. **Launch `record_server.py` pointed at it.** Either:
   ```bash
   REPLAY_RECORDER_CONFIG=$PWD/recorder.config.json \
   REPLAY_RECORDER_STATE_DIR=$PWD/_state \
       python record_server.py
   ```
   or via the CLI:
   ```bash
   replay record-server --config $PWD/recorder.config.json
   ```

4. **Connect your agent to the recorder.** From the agent's POV the
   recorder is a normal stdio MCP server: same tool names, same
   parameter schemas, same responses (the recorder forwards them
   unchanged). Run whatever rollout you want to capture.

5. **Cassettes appear in the configured path.** Each forwarded call
   appends one JSONL line to `cassette`. Duplicates are tolerated —
   no dedupe happens at record time (the replay-proxy uses
   last-wins on read; run `replay validate` to surface duplicate
   hashes if you care).

6. **Freeze + move to the replay-proxy's cassette dir for training.**
   Validate, then commit the cassette into `replay-proxy/cassettes/`:
   ```bash
   replay validate cassettes/yahoo-finance.jsonl
   replay list     cassettes/yahoo-finance.jsonl
   ```
   The training-time replay-proxy then reads it via
   `REPLAY_PROXY_CONFIG`, exactly as in the deterministic-replay
   workflow.

### Recorder state files

```
$REPLAY_RECORDER_STATE_DIR/
├── recorded_calls.jsonl   # one line per call appended to cassette
├── errors.jsonl           # upstream failures, cassette write errors
```

Default outside the container: `~/.openclaw/replay_recorder/`.

### Failure modes

* **Upstream subprocess crashes / never starts.** The recorder
  raises during startup (the agent will see the stdio session die).
  Per-call failures (timeouts, upstream raises) are caught: the
  agent receives a structured `{"error":"upstream_call_failed",
  ...}` envelope and a line is appended to `errors.jsonl` — the
  rest of the session keeps working.
* **Cassette write fails (disk full, permissions).** The call
  still succeeds for the agent; the error lands in `errors.jsonl`.
* **Ctrl-C / SIGTERM.** The recorder catches `SIGINT`/`SIGTERM`,
  cancels its serve loop, drains the upstream `ClientSession`,
  closes stdio cleanly.

### Bootstrap from an existing trajectory

If you already have an OpenAI-style or Toolathlon-style rollout in
JSONL form, you can replay it once against the real upstream and
capture deterministic cassettes for every tool call:

```bash
replay record-replay-trajectory \
    --trajectory rollouts/yahoo-task.jsonl \
    --server     yahoo-finance \
    --cassette   cassettes/yahoo-finance.jsonl \
    --upstream-cmd "uv run /path/to/upstream/server.py" \
    --upstream-env YAHOO_API_KEY=... \
    --upstream-cwd /path/to/upstream
```

Per-line trajectory shapes tolerated:

| Shape                                                          | Source            |
|----------------------------------------------------------------|-------------------|
| `{"tool":"…","args":{…}}`                                      | native            |
| `{"name":"…","arguments":{…}}`                                 | MCP-style         |
| `{"tool_calls":[{"function":{"name":"…","arguments":"…"}}]}`   | OpenAI ChatCompletions |
| `{"function":{"name":"…","arguments":…}}`                      | partial OpenAI    |

The command prints a JSON summary
(`{"ok": N, "errors": N, "skipped": N, "total": N, "cassette": "..."}`)
and exits non-zero if any step errored. Tool names not present in
the live upstream's tool list are skipped (counted in `skipped`)
rather than failing the run.

## Adding a new Tier-B server

1. Write `config-<server>.json` declaring `name`, `cassette` path,
   and one `tools` entry per upstream tool. The `name` and `params`
   must match the real server exactly — the proxy is signature-
   compatible, not just response-compatible.
2. Pre-record an initial cassette covering the (tool, args) tuples
   the task expects. Even 3–5 entries per tool is enough to start;
   the miss log will tell you what else to record.
3. Mount the config + cassette into the container and point
   `REPLAY_PROXY_CONFIG` at the config.

The Tier-B servers we want to support (each needs its own tool
schema definition):

- `yahoo-finance` (`lockon-n/yahoo-finance-mcp`) — done as example
- `fetch` (generic HTTP)
- `playwright_with_chunk` (browser snapshots)
- `scholarly`, `arxiv`, `pubmed`, `wikipedia`, `open-library`,
  `met-museum`, `clinicaltrialsgov`, `context7`
- `google_map` / `google-maps` / `osm-mcp-server` / `weather` /
  `weather-data` / `national-parks`
- `ddg-search` / `brave-search` / `exa` / `oxylabs`
- `youtube` / `youtube-transcript`
- `whois`, `lara-translate`, `balldontlie`

## Shipped per-server configs

The `configs/` directory ships starter configs + cassettes for several
Tier-B servers. Each config can be used standalone (single-server
proxy) or composed via a bundle config that lists multiple servers
in one `REPLAY_PROXY_CONFIG`.

### Knowledge bundle (`configs/knowledge-bundle.json`)

Reference/knowledge APIs — encyclopedia, biomedical literature,
preprints, books, museums, and clinical trials. All
deterministically replayed so the agent's queries can be evaluated
without rate-limited live calls.

| Server              | Upstream                                                                                                              | Tools | Cassette entries |
|---------------------|-----------------------------------------------------------------------------------------------------------------------|-------|------------------|
| `wikipedia`         | [`Rudra-ravi/wikipedia-mcp`](https://github.com/Rudra-ravi/wikipedia-mcp)                                             | 10    | 5 (Einstein search, summary, Photoelectric effect, section tree, Eiffel Tower coordinates) |
| `pubmed`            | [`geobio/PubMed-MCP-Server`](https://github.com/geobio/PubMed-MCP-Server)                                             | 4     | 5 (long-COVID kw search, GLP-1 search, Karikó advanced search, PMID metadata, PDF download) |
| `arxiv`             | [`blazickjp/arxiv-mcp-server`](https://github.com/blazickjp/arxiv-mcp-server)                                         | 4     | 5 (RLHF cs.CL search, diffusion-transformer search, download/list/read for 2203.02155) |
| `scholarly`         | [`adityak74/mcp-scholarly`](https://github.com/adityak74/mcp-scholarly) (lockon-n fork)                               | 1     | 4 (GNN drug discovery, speech ASR, in-context learning, RAG) |
| `open-library`      | [`geobio/mcp-open-library`](https://github.com/geobio/mcp-open-library)                                               | 6     | 5 (Three-Body Problem title, Le Guin author, author info, ISBN lookup, cover URL) |
| `met-museum`        | [`mikechao/metmuseum-mcp`](https://github.com/mikechao/metmuseum-mcp)                                                 | 3     | 5 (departments, Picasso search, "The Actor", Sunflowers search, Van Gogh Sunflowers) |
| `clinicaltrialsgov` | [`cyanheads/clinicaltrialsgov-mcp-server`](https://github.com/cyanheads/clinicaltrialsgov-mcp-server)                 | 7     | 5 (ACTT NCT04280705 record, GLP-1 obesity search, long-COVID count, overallStatus field values, patient eligibility match) |

Notes on response shapes:

- **`wikipedia`** — `search_wikipedia` returns `{query, results:[{title,snippet,pageid}]}`; `get_summary`/`get_article` return `{title, summary|content, pageid?, url?}` matching the upstream JSON.
- **`pubmed`** — search tools return a list of article dicts (`pmid`, `title`, `authors`, `journal`, `publication_date`, `abstract_snippet`); `get_pubmed_article_metadata` returns the full metadata dict; `download_pubmed_pdf` returns a string status, as in upstream.
- **`arxiv`** — recorded shapes follow blazickjp's tool outputs (`results:[{id,title,authors,published,categories,summary}]` for search; status/path dict for downloads; `papers` list for `list_papers`).
- **`scholarly`** — minimal one-tool surface (`search-arxiv`); response wraps `{keyword, articles:[{id,title,authors,published,summary}]}`.
- **`open-library`** — `numFound`/`docs` envelopes for searches (matches the Open Library `/search.json` API the server proxies); identifier lookups return the book/author dict directly.
- **`met-museum`** — `objectID` is integer; search responses include both `objectIDs` and a `objects` array of hydrated records; `get-museum-object` mirrors the Met's open-access JSON.
- **`clinicaltrialsgov`** — fields use the v2 API spelling (`nctId`, `briefTitle`, `overallStatus`, `phase`, `interventions`); search returns `{totalCount,nextPageToken,studies}` for cursor-paginated walks.

### Utility bundle (`configs/utility-bundle.json`)

Domain WHOIS, machine translation, and sports stats — handy for
agent tasks that need a few low-volume utility lookups without
hitting live APIs.

| Server          | Upstream                                                            | Tools                                                         | Cassette entries |
|-----------------|---------------------------------------------------------------------|---------------------------------------------------------------|------------------|
| `whois`         | [`@bharathvaj/whois-mcp`](https://github.com/bharathvaj-ganesan/whois-mcp) (`whoiser`) | `whois_domain`, `whois_tld`, `whois_ip`, `whois_as` | 5 (google.com, wikipedia.org, github.io, anthropic.ai, tld `io`) |
| `lara-translate`| [`@translated/lara-mcp`](https://github.com/translated/lara-mcp)    | `translate`, `detect_language`, `list_languages`              | 5 (en→es, en→fr, en→ja, French detect, language list) |
| `balldontlie`   | [`mikechao/balldontlie-mcp`](https://github.com/mikechao/balldontlie-mcp) | `get_teams`, `get_players`, `get_games`, `get_game`      | 4 (NBA teams, LeBron, Lakers games, single game) |

Cassette responses mirror the upstream MCP wire shapes:

- **`whois`** — `whoiser` returns a dict keyed by upstream WHOIS
  server (e.g. `"whois.verisign-grs.com"`); each value is the parsed
  field map (`Domain Name`, `Creation Date`, `Registry Expiry Date`,
  `Name Server: [...]`, ...). Field availability varies by TLD — `.io`
  and `.ai` entries are sparser than `.com`.
- **`lara-translate`** — `translate` returns the array of text blocks
  the upstream SDK exposes as `result.translation`. `detect_language`
  returns an array of `{language, confidence}` records (one per input
  string).
- **`balldontlie`** — the upstream MCP wraps everything in
  `{"content":[{"type":"text","text":"..."}]}`. We record that exact
  envelope so downstream agents see the same shape as the live tool.

Note: domain names are recorded lowercase. The shared
`CASE_INSENSITIVE_KEYS` set in `replay.cassette` does not currently
include `domain` / `tld` / `ip` / `asn`; if you re-record entries
with mixed-case inputs and want them to dedupe, add those keys
there.

### Search bundle (`configs/search-bundle.json`)

General-purpose web search and HTTP fetch — DuckDuckGo, Brave, Exa,
Oxylabs (Google SERP / Amazon scraper), and a generic HTML/JSON/text
fetcher. Together they cover the "find a page, then read it"
half-step that most agent tasks rely on, deterministically replayed
so RL rollouts don't depend on rate-limited and drifting public APIs.

| Server         | Upstream                                                                                                  | Tools                                                                                          | Cassette entries |
|----------------|-----------------------------------------------------------------------------------------------------------|------------------------------------------------------------------------------------------------|------------------|
| `ddg-search`   | [`nickclyde/duckduckgo-mcp-server`](https://github.com/nickclyde/duckduckgo-mcp-server)                   | `search`, `fetch_content`                                                                      | 5 (GPT-4 search ×3 variants, GPT-4 page fetch, Wikipedia fetch) |
| `brave-search` | [`modelcontextprotocol/servers-archived` — `src/brave-search`](https://github.com/modelcontextprotocol/servers-archived/tree/main/src/brave-search) | `brave_web_search`, `brave_local_search`                                                       | 4 (GPT-4 search ×2, Rust search, Central Park pizza local) |
| `exa`          | [`exa-labs/exa-mcp-server`](https://github.com/exa-labs/exa-mcp-server)                                   | `web_search_exa`, `web_fetch_exa`                                                              | 5 (RLHF papers ×2 variants, diffusion survey, two arxiv abstract fetches) |
| `oxylabs`      | [`oxylabs/oxylabs-mcp`](https://github.com/oxylabs/oxylabs-mcp)                                           | `universal_scraper`, `google_search_scraper`, `amazon_search_scraper`, `amazon_product_scraper` | 5 (Google "best programming language 2025" ×2, GPT-4 Google SERP, example.com universal, Amazon mechanical-keyboard search) |
| `fetch`        | [`tokenizin-agency/mcp-npx-fetch`](https://github.com/tokenizin-agency/mcp-npx-fetch) (Toolathlon `npx-fetch.yaml`) | `fetch_html`, `fetch_markdown`, `fetch_txt`, `fetch_json`                                       | 5 (example.com html/txt/markdown, httpbin JSON, github.com/anthropics/claude-code API JSON) |

Tool names and parameter names match upstream verbatim. Response
shapes mirror each upstream's wire format:

- **`ddg-search`** and **`brave-search`** return formatted text
  strings (not structured JSON) — the upstream servers shape
  results for LLM consumption (`"Found N search results:..."` for
  DDG; `"Title: ...\nDescription: ...\nURL: ..."` blocks for Brave).
- **`exa`** returns Title/URL/Published/Author/Highlights blocks
  separated by `---`; `web_fetch_exa` returns `# Title\nURL:\n...`
  markdown.
- **`oxylabs`** returns a JSON-stringified parsed-SERP body when
  `parse=true` (the default) — exact wire shape produced by
  `oxylabs_mcp.utils.get_content` with `json.dumps(content)` for
  parsed dicts. `universal_scraper` returns markdownified HTML.
- **`fetch`** returns the upstream MCP envelope verbatim:
  `{"content":[{"type":"text","text":"..."}], "isError": false}`,
  with `fetch_json` JSON-stringifying the parsed body inside the
  text field (same as upstream).

### Geo / weather bundle (`configs/geo-bundle.json`)

Maps, geocoding, and weather APIs — Google Maps Platform,
OpenStreetMap (Nominatim + OSRM), NWS (weather.gov), WeatherAPI,
and the US National Park Service. Cassettes are sized so a typical
agent task can chain geocode → route → forecast → park lookup
without any live API calls.

| Server            | Upstream                                                                                                                                                | Tools                                                                                                                              | Cassette entries |
|-------------------|---------------------------------------------------------------------------------------------------------------------------------------------------------|------------------------------------------------------------------------------------------------------------------------------------|------------------|
| `google_map`      | [`modelcontextprotocol/servers-archived` — `src/google-maps`](https://github.com/modelcontextprotocol/servers-archived/tree/main/src/google-maps)       | `maps_geocode`, `maps_reverse_geocode`, `maps_search_places`, `maps_place_details`, `maps_distance_matrix`, `maps_elevation`, `maps_directions` | 8 (Googleplex geocode, Eiffel geocode, Empire State reverse, SF ramen places, place details, SF→LA distance matrix, Denver + Whitney elevation, SF→LA directions) |
| `google-maps`     | same upstream (atlas alias)                                                                                                                             | identical tool surface to `google_map`                                                                                              | 8 (same cassette content as `google_map`, `server` field flipped) |
| `osm-mcp-server`  | [`jagan-shanmugam/open-streetmap-mcp`](https://github.com/jagan-shanmugam/open-streetmap-mcp)                                                            | `geocode_address`, `reverse_geocode`, `find_nearby_places`, `get_route_directions`, `explore_area`                                  | 6 (Times Square + Eiffel geocode, Empire State reverse, SF→LA route, Times Square cafes, Eiffel area explore) |
| `weather`         | [`geobio/smitheryai-mcp-servers-weather`](https://github.com/geobio/smitheryai-mcp-servers-weather) (weather.gov)                                        | `get_current_weather`, `get_weather_forecast`, `get_hourly_forecast`, `get_weather_alerts`, `find_weather_stations`, `get_local_time` | 7 (Seattle current, NYC current, NYC 5-day forecast, Seattle 12-hour forecast, CA red-flag alerts, Seattle stations, NYC local time) |
| `weather-data`    | [`geobio/weather-mcp-server`](https://github.com/geobio/weather-mcp-server) (weatherapi.com)                                                              | `weather_current`, `weather_forecast`, `weather_history`, `weather_alerts`, `weather_airquality`, `weather_astronomy`, `weather_search`, `weather_timezone` | 6 (Seattle + London current, Seattle 1-day forecast, Seattle 2026-05-15 history, Miami tropical-storm alert, LA AQI) |
| `national-parks`  | [`KyrieTangSheng/mcp-server-nationalparks`](https://github.com/KyrieTangSheng/mcp-server-nationalparks) (NPS API)                                         | `findParks`, `getParkDetails`, `getAlerts`, `getVisitorCenters`, `getCampgrounds`, `getEvents`                                      | 7 (Yosemite search, CA state parks, Yosemite details, Yosemite alerts, 3 campgrounds, 2 visitor centers, 2 upcoming events) |

Notes on response shapes:

- **`google_map` / `google-maps`** — share an upstream
  (`@modelcontextprotocol/server-google-maps`). The geo-bundle ships
  only `google_map` to avoid FastMCP tool-name collisions; use the
  standalone `google-maps.json` config when an environment specifies
  that alias. Responses follow the Google Maps Platform reply shape
  used by the upstream (e.g. `location: {lat, lng}`, `formatted_address`,
  `place_id`, `results`/`places` arrays).
- **`osm-mcp-server`** — Nominatim `display_name` / `lat` / `lon` /
  `boundingbox` for geocode; OSRM `code` / `routes[].distance`
  (meters) / `duration` (seconds) / `legs` for routing.
- **`weather`** — wraps responses as
  `{"content":[{"type":"text","text":"# Current Weather\n…"}]}` —
  the upstream returns markdown inside an MCP text block.
- **`weather-data`** — WeatherAPI `{location:{name,region,country,…},
  current:{temp_c,temp_f,condition:{text,code,icon},…}}` envelope;
  forecast/history adds `forecast.forecastday[].day` + `astro`;
  AQI adds `current.air_quality`.
- **`national-parks`** — NPS v1 API `{total, limit, start, data:[…]}`
  envelope; `parkCode` is the slug-style id; alerts/campgrounds
  reuse the same envelope.

### Media + browse bundle (`configs/media-bundle.json`)

YouTube data, transcripts, library documentation, and the chunked
Playwright browser. The four servers most often used together when an
agent needs to find a video, read its transcript, look up library
docs, and verify content via a real browser snapshot.

| Server                  | Upstream                                                                                                                                       | Tools | Cassette entries |
|-------------------------|------------------------------------------------------------------------------------------------------------------------------------------------|-------|------------------|
| `youtube`               | [`lockon-n/youtube-mcp-server`](https://github.com/lockon-n/youtube-mcp-server) (fork of `ZubeidHendricks/youtube-mcp-server`)                  | 9     | 4 (MrBeast search, video detail, channel detail, transcript) |
| `youtube-transcript`    | [`jkawamoto/mcp-youtube-transcript`](https://github.com/jkawamoto/mcp-youtube-transcript)                                                       | 4     | 5 ("Me at the zoo" text + timed transcript + video info + languages, Rickroll transcript) |
| `context7`              | [`upstash/context7-mcp@1.0.14`](https://github.com/upstash/context7/tree/v1.0.14) (version pinned by mcp-atlas template)                         | 2     | 4 (react resolve, Next.js resolve, /facebook/react hooks docs, /vercel/next.js routing docs) |
| `playwright_with_chunk` | [`lockon-n/playwright-mcp`](https://github.com/lockon-n/playwright-mcp) (chunking fork of `microsoft/playwright-mcp`)                            | 35    | 7 (example.com navigate + snapshot, playwright.dev/docs/intro navigate + 3 spans + 1 regex search) |

Notes on response shapes + chunking semantics:

- **`youtube`** — tool names use underscore separators (`videos_getVideo`,
  `videos_searchVideos`, `transcripts_getTranscript`,
  `channels_getChannel`, `channels_listVideos`, `channels_navigateList`,
  `playlists_getPlaylist`, `playlists_getPlaylistItems`,
  `playlists_searchPlaylists`). Responses are YouTube Data API v3 shape
  with thumbnails stripped (upstream's `removeThumbnails()` utility).
  `transcripts_getTranscript` returns
  `{videoId, language, transcript:[{text,offset,duration}]}` from the
  `youtube-transcript` npm package — note `offset` is in **milliseconds**,
  not the seconds-based `start` used by the separate
  `youtube-transcript` MCP server.
- **`youtube-transcript`** — `get_transcript` returns
  `{title, transcript:"…\n…", next_cursor}` where `transcript` is a
  newline-joined string. `get_timed_transcript` returns
  `{title, snippets:[{text, start (seconds), duration (seconds)}],
  next_cursor}`. `get_video_info` returns yt-dlp-derived metadata.
  `get_available_languages` returns a list of human-readable language
  labels (e.g. `"English (en)"`). Pagination via `next_cursor` only
  kicks in when `--response-limit` is set.
- **`context7`** — pinned to **1.0.14** because that's the version in
  the mcp-atlas template. Tools: `resolve-library-id` (param
  `libraryName`) and `get-library-docs` (params
  `context7CompatibleLibraryID`, `topic`, `tokens`). The 2.x series
  renamed `get-library-docs` to `query-docs`, added a `query` param to
  `resolve-library-id`, and dropped `tokens`. Re-cassette before
  bumping the pin. Responses are wrapped in the MCP
  `{content:[{type:"text", text:"…"}]}` envelope — text payload is
  formatted-library-list markdown for `resolve-library-id` and free-form
  documentation markdown for `get-library-docs`.
- **`playwright_with_chunk`** — `lockon-n`'s chunking fork of
  `microsoft/playwright-mcp`. **Chunking contract** (this is the
  important part):
    - `browser_snapshot` captures the page's accessibility tree as YAML
      and slices it into sequentially-numbered **spans** of
      `--span-size` characters (default 2000, Toolathlon configures
      **5000**). The tool returns the **first** span only and bundles
      a snapshot-state summary (current span index + total span count).
    - Subsequent spans are reached via the seven span-navigation tools:
      `browser_snapshot_navigate_to_first_span`, `_to_last_span`,
      `_to_next_span`, `_to_prev_span`, `_to_span` (with `spanIndex`,
      0-based), `_to_line` (with `globalLineNumber`, 1-based, plus
      optional `contextLines` in [0, 10], default 3), and
      `browser_snapshot_search` (regex `pattern` + optional `flags`).
    - `browser_snapshot_search` returns *match locations* with both
      global line numbers (across the whole snapshot) and in-span line
      numbers, plus the set of span indices containing matches; it
      does not change the current span.
    - All responses use the MCP wire envelope
      `{content:[{type:"text", text:"# Result\n…\n```yaml\n…\n```\n"}]}`
      — `### Result` for navigation/search status, `### Ran Playwright
      code` for the JS that would have been executed,
      `- Page URL`/`- Page Title`/`- Page Snapshot (span X of Y …)`
      for snapshot output, then a fenced `yaml` block with the
      accessibility tree slice.
    - Cassette cost: one cassette entry per `(URL, span)` plus one per
      `browser_navigate`. Page that yields 5 spans = 6 entries to
      cover a top-to-bottom walk, plus extra entries for any
      `browser_snapshot_search` queries. Plan recording sessions
      accordingly — this server's cassette will be ~5× the size of
      a comparable read-only API.
- **All four** — recorded responses are stored exactly as the upstream
  tool returns them on the wire (deserialized JSON from the MCP
  `tools/call` result), which is what an in-process recorder hook
  captures via `replay.cassette.write_entry`.

Standalone configs are also shipped for each server
(`configs/youtube.json`, `configs/youtube-transcript.json`,
`configs/context7.json`, `configs/playwright_with_chunk.json`) for
tasks that only need one of them.
