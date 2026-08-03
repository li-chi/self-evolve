"""Distil reusable experience from N rollouts of a task, using a stronger model.

Reads only our own qwen traces — never the grader, never external data. The
teacher is asked to do two things in one pass:

  1. judge each trace (did it satisfy the instruction?), which is the utility
     signal the environment never gives us, and which we can score against
     `reward.txt` afterwards to find out whether the judge is trustworthy;
  2. distil guidance concise enough to prepend to the next run.

Labels are withheld deliberately. If the teacher can separate the successes
from the failures unaided, the judge is usable on tasks where no grader exists
— which is the whole point.

    .venv/bin/python -m tools.evolve.teacher game-statistics --job big-log
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from tools.evolve import canon, contract, mine, replay

MODEL = os.environ.get("EVOLVE_TEACHER_MODEL", "claude-opus-5")
KEY_SEARCH = [Path.home() / "Projects" / p / ".env"
              for p in ("coding-env", "midtrain")]


def api_key() -> str:
    if os.environ.get("ANTHROPIC_API_KEY"):
        return os.environ["ANTHROPIC_API_KEY"]
    for env in KEY_SEARCH:
        if not env.exists():
            continue
        for line in env.read_text().splitlines():
            if line.startswith("ANTHROPIC_API_KEY="):
                return line.split("=", 1)[1].strip().strip("'\"")
    raise SystemExit("no ANTHROPIC_API_KEY found")


PROMPT = """\
Below are {n} independent attempts by a small model at the SAME task, in the \
same environment. Each is a transcript of shell commands and their output. You \
are NOT told which attempts succeeded — a grader exists but you cannot see it.

Do two things.

1. JUDGE each attempt. Work only from evidence in its transcript: for every \
outcome the task requires, decide whether the transcript shows it actually \
happened. An agent claiming completion is not evidence. Give a verdict of \
"pass" or "fail" and one sentence of reasoning naming the deciding evidence.

2. DISTIL what separates the attempts that satisfied the task from those that \
did not, as guidance for a future attempt at a DIFFERENT task in this same \
environment. It will be prepended to that run, so it must be:
   - short: at most 8 bullets, each one line
   - actionable: a rule the agent can follow, not an observation
   - transferable: about this environment's tools and about how to work, not \
about this task's specific tables or values
   - grounded: only claim what the transcripts show

Do not include anything a competent agent already knows. If the attempts \
differ only by luck, say so rather than inventing a pattern.

Return JSON only:
{{"verdicts": [{{"id": "<id>", "verdict": "pass"|"fail", "why": "<one sentence>"}}],
  "guidance": ["<bullet>", ...],
  "confidence": "high"|"medium"|"low"}}

{traces}
"""


def build(task: str, job: str | None, level: str) -> tuple[str, dict[str, float]]:
    # Read the ledger by path so a single job can be isolated; mixing jobs
    # would mix experimental conditions into one distillation.
    root = replay.STORE / "log"
    files = sorted((root / job).glob("*.jsonl")) if job else sorted(root.rglob("*.jsonl"))
    turns = []
    for f in files:
        recs = [r for r in replay.reconstruct(f) if r["role"] == "main"]
        if not recs or recs[0]["task"] != task:
            continue
        logs = recs[0].get("logs_dir")
        reward = replay.reward_for(Path(logs)) if logs else None
        for i, r in enumerate(recs):
            obs = ""
            if i + 1 < len(recs):
                for m in reversed(recs[i + 1]["new_messages"]):
                    if m.get("role") == "user":
                        obs = m.get("content", "")
                        break
            turns.append(replay.Turn(r["task"], r["run"], r["turn"], "main",
                                     r["messages"], r["response"],
                                     replay._commands(r["response"]), obs,
                                     reward, "ledger"))
    rolls = canon.rollouts_of(turns)
    ct = contract.learn(list(replay.iter_trajectory_turns(
        exclude_jobs=set(mine.INTERVENED))))
    boiler = canon.learn_boilerplate(canon.rollouts_of(
        [t for t in replay.iter_ledger_turns(arm="log") if t.role == "main"]))

    blocks, truth = [], {}
    for i, (key, v) in enumerate(sorted(rolls.items()), 1):
        rid = f"attempt-{i}"
        truth[rid] = v[0].reward
        blocks.append(f"\n===== {rid} =====\n"
                      + canon.canonical(v, ct, boiler, level=level))
    return PROMPT.format(n=len(blocks), traces="\n".join(blocks)), truth


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 1
    task = argv[1]
    job = argv[argv.index("--job") + 1] if "--job" in argv else None
    level = argv[argv.index("--level") + 1] if "--level" in argv else "L2"

    prompt, truth = build(task, job, level)
    print(f"# {task}: {len(truth)} attempts, prompt ~{len(prompt)//4:,} tokens",
          file=sys.stderr)

    import anthropic

    client = anthropic.Anthropic(api_key=api_key())
    with client.messages.stream(
        model=MODEL,
        max_tokens=32000,
        output_config={"effort": "high"},
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        msg = stream.get_final_message()

    text = next((b.text for b in msg.content if b.type == "text"), "")
    print(f"# usage: in={msg.usage.input_tokens:,} out={msg.usage.output_tokens:,}"
          f"  cost≈${msg.usage.input_tokens*5e-6 + msg.usage.output_tokens*25e-6:.2f}",
          file=sys.stderr)

    start, end = text.find("{"), text.rfind("}")
    data = json.loads(text[start:end + 1])

    hits = 0
    print(f"\n{'attempt':12} {'teacher':>8} {'grader':>8}")
    for v in data["verdicts"]:
        g = truth.get(v["id"])
        t = 1.0 if v["verdict"] == "pass" else 0.0
        hits += t == g
        print(f"{v['id']:12} {v['verdict']:>8} {'pass' if g == 1.0 else 'fail':>8}"
              f"{'' if t == g else '   <-- disagree'}")
    print(f"\njudge agreement with grader: {hits}/{len(data['verdicts'])}")
    print(f"confidence: {data.get('confidence')}\n\nDISTILLED GUIDANCE:")
    for b in data["guidance"]:
        print(f"  - {b}")
    Path("jobs/_cards").mkdir(parents=True, exist_ok=True)
    out = Path("jobs/_cards") / f"_teacher_{task}.json"
    out.write_text(json.dumps({"task": task, **data, "truth": truth}, indent=1))
    print(f"\nwritten: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
