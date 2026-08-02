# self-evolve

Evaluation infrastructure for self-evolving agents, built on the
[Harbor](https://harborframework.com) framework. The first benchmark being
ported is [Toolathlon](https://github.com/lockon-n/toolathlon) (expected
locally at `~/Projects/Toolathlon`).

The goal is **self-contained tasks**: every task runs from a single image
with no logged-in account behind it, while keeping the upstream task shape —
same instruction, same initial environment, same preprocess, same grader.
Services that require a login are replaced by mock MCP servers whose tool
surface matches the real one; genuinely live data (public web search, market
quotes, timetables) stays live, because those key spaces cannot be
enumerated into a fixture.

See **[MOCK_TRACK.md](MOCK_TRACK.md)** for how a mocked service keeps
upstream preprocess and upstream graders running verbatim, and
**[PORTING.md](PORTING.md)** for the fidelity contract and porting recipe.

## Layout

```
mocks/                  # ~58 mock MCP servers (state_json tier) mirroring the
                        # tool surfaces of the real servers Toolathlon uses
  gcp-sdk-shim/         # google.cloud.* client shim over the mock's state, so
                        # upstream preprocess/graders run unmodified
  poste-mock/           # self-contained SMTP + IMAP4rev1 server replacing
                        # poste.io (the real emails-mcp runs against it)
  woocommerce-mock/     # + rest_facade.py: the WooCommerce/WordPress REST API
                        # served from the same mock state
  mcp-bridge/           # mcp-tool: shell access to MCP servers (+ runtime
                        # rendering of per-task service scopes)
base-image/             # toolathlon-harbor-base:v2 (plain) / :v3 (mock track)
datasets/toolathlon/    # tasks ported to the Harbor task format
  <task>/
    task.toml           # Harbor task config (schema 1.3)
    instruction.md      # upstream docs/task.md (+ tools appendix on mock tasks)
    environment/        # Dockerfile, init.sh, task subtree, mcp/, mock_seed/
    tests/              # test.sh + the upstream grader package
    solution/           # oracle solution (groundtruth ships only here)
tools/                  # port_task.py, mock_track.py, build_base_image.sh
jobs/                   # harbor run outputs (gitignored)
```

## Running

```bash
# build the mock-track base image (stages mocks + shims into the context)
./tools/build_base_image.sh

# port a task (add --mock <service> for the mock track)
python3 tools/port_task.py ab-testing --mock google-cloud

# validate: oracle must score 1.0, nop must score 0.0
.venv/bin/harbor run -p datasets/toolathlon/<task> -a oracle
.venv/bin/harbor run -p datasets/toolathlon/<task> -a nop

# self-hosted qwen baseline, 5 rollouts
.venv/bin/harbor run -p datasets/toolathlon/<task> -a terminus-2 \
  -m openai/qwen3.6-35b -k 5 -n 16 --env-file .env
```

Model endpoints live in `.env` (gitignored): the self-hosted SGLang/LiteLLM
proxy at `https://glm.analogyai.ai/v1`, model `qwen3.6-35b`.

## Rollout throughput

The GPU server batches well; the limits are all client-side. Findings from
the first 119 rollouts (2026-08-01):

- **Run batches with `-n 16`.** Harbor's default is 4 and earlier batches
  peaked at 6 concurrent trials (effective concurrency ~5). Task containers
  are capped at 1 CPU / 2 GB each (task.toml) and the Docker Desktop VM has
  20 CPUs / 64 GB, so the host machine was idle by allocation — the trial
  semaphore was the bottleneck, and each trial's serial ~3.4 s API calls
  left the GPU underused.
- **tmux + asciinema are baked into the base images.** terminus-2 installs
  them per trial otherwise: 52 s mean agent_setup across the 119 rollouts,
  and under concurrent apt load the 240 s install budget expires — which is
  what killed the whole `qwen5x-all` batch (trials proceed without
  recording, or die in setup). With the tools pre-baked the install
  short-circuits in one exec.
- Phase profile before the fix (n=119): environment_setup 17 s mean,
  agent_setup 52 s, agent_execution 426 s, verifier 19 s.

Measured after both fixes (20 trials, 4 tasks × k=5, `-n 16`, 2026-08-01):
makespan **6.2 min** (the same mix ran at ~1.6 min/trial throughput
before, ~5× slower), agent_setup **1.9 s** mean (was 52 s), 16 trials
genuinely simultaneous, effective concurrency 9.8 (the tail is one long
ab-testing rollout). Rewards match the earlier baseline exactly
(excel-data-transformation 5/5, machine-operating 5/5, ppt-analysis 4/5,
ab-testing 0/5), so the speedup did not change task behaviour.

## Track assignment

- **Login-gated service** (GCP, GitHub, Notion, Google Sheets, W&B,
  HuggingFace, WooCommerce, Snowflake…) → **mock**, with the mock's tool
  surface matched to the real server.
- **Live public data** (web search, fetch, market quotes, train timetables)
  → **live**. Search-shaped tasks in particular cannot be mocked without
  changing what the task measures.
- **Self-hosted services** (Canvas, Poste/email, kind) → sidecar or mock;
  not yet ported.

## Ported tasks

Validation status and the qwen3.6-35b baseline (terminus-2, 5 rollouts per
task) are tracked in **[COVERAGE.md](COVERAGE.md)**.
