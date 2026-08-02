# Toolathlon → Harbor coverage

Source: 108 tasks in `Toolathlon/tasks/finalpool` (classified programmatically;
see `task_classification.json`). Status as of 2026-08-01.

**Task directories generated: 56 / 108. Validated (oracle 1.0 / nop 0.0): 41.**
Every validated task runs from one image with no logged-in account behind it,
except where the task's subject matter *is* live public data (see "live by
design"). The other 22 generated directories run their upstream grader and
discriminate correctly (nop → 0.0) but do not yet have a working oracle —
listed under "generated, oracle pending".

## Validation

Bar: `oracle → 1.0` and `nop → 0.0`, using the upstream grader unmodified.

### Mock track — emails / poste.io (3 validated, backend done)

`mocks/poste-mock/mailserver.py` is a self-contained SMTP + IMAP4rev1 server
on the ports upstream's poste.io uses, with accounts loaded from Toolathlon's
own `users_data.json`. Nothing above it changes: upstream preprocess delivers
the seed mail through `smtplib`, upstream graders read mailboxes through
`imaplib`, and the agent drives the **genuine** `emails-mcp` server (installed
from lockon-n/emails-mcp into its own venv) pointed at localhost.

| task | oracle | nop |
|---|---|---|
| course-assistant | 1.0 | 0.0 |
| apply-phd-email | 1.0 | 0.0 |
| git-bug-hunt | 1.0 | 0.0 |

The mail-sending oracle pattern is established: solve.py drives the real
emails-mcp server via `mcp-tool call emails send_email` (attachments
resolve against /app). apply-phd-email additionally unpacks its shipped
reference tree verifier-side (tests/pre_grade.sh, pure decompression).

This unblocks the 24-task emails cluster: what remains per task is the
oracle and, where the task also touches another service, that service's
backend.

### Mock track — woocommerce (5 validated, backend done)

`mocks/woocommerce-mock/rest_facade.py` serves the store over HTTP out of
the woocommerce mock's state: `/wp-json/wc/v3` products (with variations,
attributes and attribute terms), orders, customers, categories, coupons and
the batch endpoints, plus the `/wp-json/wp/v2/media` library and the
`wp-login.php` cookie flow preprocess uses to upload images. Any `/storeNN`
prefix is accepted, as on upstream's single multi-store host. Both clients
take a URL — the agent's woocommerce MCP server and upstream's
`WooCommerceClient(site_url, ...)` — so neither is modified, and the MCP
tools and the REST facade read one store.

| task | oracle | nop |
|---|---|---|
| woocommerce-update-cover | 1.0 | 0.0 |
| inventory-sync | 1.0 | 0.0 |
| woocommerce-new-product (woocommerce + emails) | 1.0 | 0.0 |
| filter-low-selling-products (woocommerce + emails) | 1.0 | 0.0 |
| woocommerce-new-welcome (woocommerce + emails + google-cloud) | 1.0 | 0.0 |

What the four new validations needed (all fixed 2026-08-01):

- inventory-sync: the update-cover pattern one level up — preprocess's
  product mapping (region -> local id -> WC id) is re-derived at verify
  time from the store's own SKU/meta records (`tests/derive_wc_config.py`).
- woocommerce mock fidelity: caller-supplied `date_created`/`date_completed`
  on products and orders are now honoured (WC REST v3 behaviour — upstream
  preprocess backdates fixtures with it; before, every product/order was
  stamped `now`, so "in stock > 90 days" style filters matched nothing).
- tests/pkg now ships initial_workspace (port_task.py fixed): graders read
  email/blog templates out of it at verify time; the agent already has the
  same files in /app, so nothing leaks.
- gcp shim: `ScalarQueryParameter`/`QueryJobConfig.query_parameters` are
  supported (parameterised grader queries), and both the shim and the
  google-cloud mock now merge the state skeleton over a partially-written
  state.json instead of KeyError'ing.
- oracle plumbing: `mcp-tool call` output is one JSON document per content
  item (newline-joined), or plain text for tools like `send_email` — the
  solve.py helpers parse accordingly.

Remaining woocommerce-cluster tasks: update-material-inventory
(google_sheet fixtures), woocommerce-customer-survey / product-recall
(google_forms OAuth), woocommerce-stock-alert (google_sheet).

Upstream's preprocess runs verbatim against it: 3 variable products, 3
variations, 6 media items and 44 historical orders are created through the
API exactly as they would be against a real WooCommerce install.

### Mock track — google-cloud (7 tasks)

Upstream `preprocess/main.py` and `evaluation/main.py` run **verbatim**
against `mocks/google-cloud-mock` through the `google.cloud.*` shim; the
agent drives the same MCP tool surface via `mcp-tool`. See MOCK_TRACK.md.

| task | oracle | nop |
|---|---|---|
| ab-testing | 1.0 | 0.0 |
| academic-warning | 1.0 | 0.0 |
| flagged-transactions | 1.0 | 0.0 |
| game-statistics | 1.0 | 0.0 |
| live-transactions | 1.0 | 0.0 |
| machine-operating | 1.0 | 0.0 |
| price-comparison | 1.0 | 0.0 |

### Local and live-web, added this round (8 tasks)

| task | track | oracle | nop |
|---|---|---|---|
| course-schedule | local | 1.0 | 0.0 |
| logical-datasets-collection | local + arxiv/scholarly | 1.0 | 0.0 |
| mrbeast-analysis | live: youtube | 1.0 | 0.0 |
| find-alita-paper | live: arXiv | 1.0 | 0.0 |
| add-bibtex | live: scholarly + browsing | 1.0 | 0.0 |
| hk-top-conf | live: browsing | 1.0 | 0.0 |
| identify-all-songs | live: youtube + browsing | 1.0 | 0.0 |
| nvidia-market | live: yfinance + browsing | 1.0 | 0.0 |

### Local — no external service (16 tasks)

| task | oracle | nop |
|---|---|---|
| arrange-workspace | 1.0 | 0.0 |
| cooking-guidance | 1.0 | 0.0 |
| courses-ta-hws | 1.0 | 0.0 |
| detect-revised-terms | 1.0 | 0.0 |
| dietary-health | 1.0 | 0.0 |
| excel-data-transformation | 1.0 | 0.0 |
| excel-market-research | 1.0 | 0.0 |
| imagenet | 1.0 | 0.0 |
| interview-report | 1.0 | 0.0 |
| paper-checker | 1.0 | 0.0 |
| ppt-analysis | 1.0 | 0.0 |
| privacy-desensitization | 1.0 | 0.0 |
| reimbursement-form-filler | 1.0 | 0.0 |
| sales-accounting | 1.0 | 0.0 |
| university-course-selection | 1.0 | 0.0 |
| git-milestone | 1.0 | 0.0 |
| git-repo (live github search; grading is local) | 1.0 | 0.0 |

### Live by design (5 tasks)

Public data whose key space cannot be enumerated into a fixture: market
quotes and railway timetables. Mocking these would change what the task
measures, and no login is involved, so they stay live.

| task | live source | oracle | nop |
|---|---|---|---|
| yahoo-analysis | yfinance | 1.0 (recomputed at solve time) | 0.0 |
| stock-build-position | yfinance | 1.0 | 0.0 |
| travel-exchange | yfinance FX | 1.0 | 0.0 |
| train-ticket-plan | 12306 | n/a — the grader re-queries the live timetable, so no stored answer exists; the bar is nop → 0.0 with a sensible grader mismatch | 0.0 |

## Baseline: qwen3.6-35b (self-hosted SGLang via LiteLLM), terminus-2, k=5

### Mock track — google-cloud

| task | rollouts | mean reward | per-rollout |
|---|---|---|---|
| machine-operating | 5 | 1.00 | 1,1,1,1,1 |
| academic-warning | 5 | 0.80 | 1,1,1,0,1 |
| flagged-transactions | 5 | 0.80 | 1,1,1,1,0 |
| price-comparison | 5 | 0.80 | 1,1,1,0,1 |
| live-transactions | 5 | 0.40 | 0,0,0,1,1 |
| ab-testing | 5 | 0.00 | 0,0,0,0,0 |
| game-statistics | 5 | 0.00 | 0,0,0,0,0 |

The two zeros are agent shortfalls, not environment defects: on ab-testing
the model filled `record.csv` but never created the `promo-assets-for-b*`
bucket the winning variant requires; on game-statistics it updated
`player_historical_stats` correctly (two of the three grader checks pass)
but never created the `leaderboard_YYYYMMDD` table.

### Local and live tasks

| task | rollouts | mean reward |
|---|---|---|
| excel-data-transformation | 5 | 1.00 |
| ppt-analysis | 5 | 1.00 |
| sales-accounting | 5 | 1.00 |
| imagenet | 5 | 0.80 |
| excel-market-research | 5 | 0.60 |
| reimbursement-form-filler | 5 | 0.40 |
| cooking-guidance | 5 | 0.20 |
| courses-ta-hws | 5 | 0.20 |
| arrange-workspace | 5 | 0.00 |
| detect-revised-terms | 5 | 0.00 |
| dietary-health | 5 | 0.00 |
| interview-report | 5 | 0.00 |
| paper-checker | 5 | 0.00 |
| privacy-desensitization | 5 | 0.00 |
| university-course-selection | 5 | 0.00 |

Sanity signal from the trajectories: on the mock tasks the agent discovers
the service surface by itself — a typical ab-testing rollout issues
`mcp-tool tools google-cloud` (7×), `mcp-tool schema …` (7×) and 38 tool
calls — so the bridge presents a usable, discoverable surface rather than
one the prompt has to spell out.

## Service backends: status after this round

One HTTP process (`mocks/api-facade`) impersonates every API whose client
dials a hardcoded host, and `mocks/netredirect` — a `sitecustomize` on
PYTHONPATH — rewrites those hosts onto it for `requests`, `httpx`,
`httplib2` and `urllib`. Upstream client code is untouched; the mock MCP
server and the facade read one state file per service.

| service | backend | state |
|---|---|---|
| google-cloud | client shim | **working**, 7 tasks validated |
| emails / poste | protocol server (SMTP+IMAP) | **working**, 1 task validated |
| woocommerce | REST facade | **working**, 1 task validated |
| github | REST facade | **working** — verified end to end with upstream's own `utils.app_specific.github` client (login, create repo, read/write contents, issues) |
| notion | REST facade | router written, reachable; tasks need their source-page fixtures |
| huggingface | REST facade | router written; `huggingface-upload` needs its repo seed |
| google sheets + drive | REST facade + OAuth stub | router written and reachable (mock Google user credentials now ship in the image); tasks need their source-spreadsheet fixtures |
| google maps / calendar / forms | REST facade | routers written; calendar task reaches the grader |
| wandb | client shim (`wandb.Api`) | shim written; both wandb tasks reach the grader |
| snowflake | client shim (DB-API over the mock's executor) | shim written, untested against a task |

A `nop` sweep over the 34 newly generated tasks: **12 environments come up
and run their grader**; 13 fail in preprocess because the task expects
account state that has no fixture yet (a specific source spreadsheet, a
Notion template page, a seeded repo). That is fixture work per task, not
backend work.

## Keeping the answer out of the container

Upstream runs preprocess on the harness side, so anything it writes into
`groundtruth_workspace` is invisible to the agent. The port runs preprocess
*inside* the task container, so that directory would sit next to the agent —
and for several tasks it holds the actual answer (woocommerce-update-cover's
`expected_results.json`, machine-operating's computed report).

Two rules close this:

1. init.sh carries over only the **resource names** the grader and
   preprocess must agree on (randomised bucket / log-bucket names,
   `task_date`); everything else preprocess wrote there is destroyed with
   the staged task tree.
2. Where the grader needs a computed groundtruth, `tests/pre_grade.sh`
   re-derives it **at verify time** from the service's own end state — see
   `datasets/toolathlon/woocommerce-update-cover/tests/derive_expected.py`,
   which recomputes best-selling variation per product from the order
   history, the same definition upstream's preprocess used.

## Generated, oracle pending (22 tasks)

These have a task directory, a working environment and the upstream grader
wired up (nop → 0.0), but no oracle yet. Two reasons:

- **live answer** — the expected output only exists relative to today's web
  (`cvpr-research`, `language-school`, `nvidia-stock-analysis`,
  `shopping-helper`, `profile-update-online`, `academic-pdf-report`,
  `ipad-edu-price`, `trip-adviser`, `trip-itinerary-generator`,
  `subway-planning`, `search-ca-school`): upstream ships no groundtruth, so
  the oracle has to solve the task live, like the agent.
- **service backend still missing** for a second service the task touches
  (`gdp-cr5-analysis`, `inter-final-performance-analysis`,
  `llm-training-dataset`, `vlm-history-completer` → google_sheet;
  `notion-movies` → notion; `fillout-online-forms` → google_forms OAuth;
  `latex-prompt-box` → hand-written LaTeX edit). `apply-phd-email` and
  `git-bug-hunt` moved to validated (email-sending oracle pattern).

## Not yet generated: 52 tasks

### Backends still to write (52 tasks behind them)

Same recipe as google-cloud: the mock MCP server already exists; what is
missing is the client-library shim upstream's preprocess/grader import, plus
seeds and oracles.

Three backend patterns are now proven and reusable:

1. **client-library shim** over the mock's state (google-cloud)
2. **protocol server** the real clients dial (emails/poste: SMTP + IMAP)
3. **REST facade** over the mock's state, for services whose clients take a
   base URL (woocommerce)

| service | tasks | backend needed | pattern |
|---|---|---|---|
| google_sheet | 10 | `gspread` + Drive `googleapiclient` over google-sheets-mock / google-drive-mock | 1 |
| notion | 8 | notion client over notion-mock | 1 or 3 |
| woocommerce | 8 | **done** — port the tasks | 3 |
| github | 4 | GitHub REST facade over github-mock (`utils.app_specific.github` speaks `requests`); probed 2026-08-01: `git-repo` validated (grading is local, search stays live); `sync-todo-to-readme` needs a LUFFY repo seed + git-over-HTTP; `personal-website-construct` needs an academicpages repo snapshot in the seed; `dataset-license-issue` needs the hf facade to serve the user's own datasets while public license lookups stay live (mock-or-passthrough split) | 3 |
| snowflake | 4 | snowflake connector shim over snowflake-mock | 1 |
| huggingface | 4 | `huggingface_hub` over huggingface-mock | 1 or 3 |
| google_map | 6 | Maps API facade over google-maps-mock | 3 |
| wandb | 3 | `wandb.Api` over wandb-mock, seeded from the public `mluo/deepscaler-1.5b` project | 1 |
| google_calendar / google_forms | 4 | Google API facade + OAuth stub | 1 |

### Blocked on heavier infrastructure

| blocker | tasks | unblocking path |
|---|---|---|
| `canvas` | 8 | Canvas LMS sidecar (slow cold start; wants a pre-seeded image) or a canvas REST facade (pattern 3) |
| `playwright_with_chunk` | 24 | live browsing; playwright + chromium are already in the base image, so this is mechanical but heavy |
| `k8s` (kind) | 5 | DinD/kind inside the task container |
| `scholarly`, `youtube-transcript`, `arxiv-latex`, misc | ~8 | local MCP servers or replayable public APIs; low effort |

(Tasks with several blockers are counted once per blocker. `playwright`
tasks are no longer blocked — chromium is in the base image and 13 of them
are already generated; what they need is oracles.)

## Known gaps in what is already ported

- **16 local + 3 live tasks use the v1 layout**: the workspace is baked at
  build time and `launch_time` is frozen at port time (now at least in
  upstream's `%Y-%m-%d %H:%M:%S %A` shape). Upstream runs preprocess at
  launch with the real time. Tasks whose grading depends on elapsed time
  should be regenerated with the v3 generator, which does this correctly.
- **Trajectory-reading graders**: `test.sh` supplies
  `{"config": {"launch_time": …}}` — real harness metadata, nothing invented.
  A grader that genuinely grades trajectory *content* would still need a
  bridge; none of the ported tasks do.
- **MCP-native agents**: mock tasks expose the servers through `mcp-tool`.
  Wiring the same servers into `[[environment.mcp_servers]]` for MCP-capable
  agents is still to do.

## Deliberately out of scope

- Multi-turn / LLM-user-simulator mode (near-universal single-turn upstream).
- `pass: null` semantics (harness failure) — maps to Harbor exceptions.
- Partial credit — upstream is strictly binary; Harbor's reward.json enables
  it later without re-porting.
