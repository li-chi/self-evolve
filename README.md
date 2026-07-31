# self-evolve

Evaluation infrastructure for self-evolving agents, built on the
[Harbor](https://harborframework.com) framework. The first benchmark being
ported is [Toolathlon](https://github.com/lockon-n/toolathlon) (expected
locally at `~/Projects/Toolathlon`).

## Layout

```
mocks/                  # ~58 mock MCP servers (state_json tier) + replay-proxy,
                        # mirroring the tool surfaces of the real servers
                        # Toolathlon uses. Used for the hermetic "mock" track.
datasets/
  toolathlon/           # Toolathlon tasks ported to Harbor task format
    <task-name>/
      task.toml         # Harbor task config (schema 1.3)
      instruction.md    # agent prompt (from Toolathlon docs/task.md)
      environment/      # Dockerfile (+ seed state for mock-track tasks)
      tests/            # test.sh + grader.py (original Toolathlon grader,
                        # exit code mapped to /logs/verifier/reward.txt)
      solution/         # oracle solution (groundtruth ships here — only the
                        # oracle agent uploads this dir into the container)
.venv/                  # project-local venv with the `harbor` CLI
jobs/                   # harbor run outputs (gitignored)
```

## Running

```bash
# oracle must score 1.0; nop must score 0.0
.venv/bin/harbor run -p datasets/toolathlon/<task> -a oracle
.venv/bin/harbor run -p datasets/toolathlon/<task> -a nop

# a real agent
.venv/bin/harbor run -p datasets/toolathlon/<task> -a claude-code -m <model>
```

## Porting rules (agreed design)

Where does a task's ground truth live?

- **Private mutable account state** (GitHub/Notion/Google/Snowflake/HF/W&B
  writes) → **mock track**: the `state_json` mock MCP servers in `mocks/`,
  seeded per task, verifier reads the mock's end-state JSON.
- **Public web** (search, fetch, wikipedia, live browsing) → **live**,
  regardless of whether an API key is needed. Unlimited key spaces cannot be
  enumerated into cassettes.
- **Self-hosted services** (Canvas, Poste, WooCommerce, kind) → live
  (self-hosted) first; mock later only if concurrency demands it.
- **Dual-nature tasks** (read public web through an authenticated service and
  also write private state) → case-by-case: snapshot the public objects into
  the seed, or keep the task on the live track.

Scoring: Toolathlon graders signal pass/fail by exit code; `tests/test.sh`
maps that to reward 1/0. Graders are copied verbatim where self-contained;
tasks whose graders import Toolathlon's `utils.*` vendor those modules into
`tests/`.

## Ported tasks

| task | track | oracle | nop |
|---|---|---|---|
| privacy-desensitization | local (no external services) | 1.0 | 0.0 |
