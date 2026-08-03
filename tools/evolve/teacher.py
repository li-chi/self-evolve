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

Work in three steps.

STEP 1 — JUDGE each attempt. For every outcome the task requires, decide \
whether the transcript shows it actually happened. An agent claiming completion \
is not evidence. Verdict "pass" or "fail", plus one sentence naming the \
deciding evidence.

STEP 2 — READ each attempt in light of its own verdict.
  For each attempt you judged FAIL: name the single decisive fault — the point \
  where it diverged from a run that would have satisfied the task — and the \
  rule that would have prevented it.
  For each attempt you judged PASS: name the step that produced the required \
  outcome and that a failing attempt omitted or botched.

STEP 3 — DISTIL guidance for a future attempt at a DIFFERENT task in this same \
environment. For each rule, cite attempts on BOTH sides with their polarity:
  "complied" — attempts that followed the rule (positive evidence)
  "violated" — attempts that broke it (counter-evidence)
An attempt appearing on both sides of your evidence is expected and good: the \
passes show the rule being followed, the failures show the cost of breaking it. \
What makes a rule worthless is a violator that succeeded anyway. Class each:
  "discriminative" — violating it cost the run the task.
  "procedure"      — a step the passes share and the failures lack.
  "contract"       — a fact about the tools every attempt hit regardless of \
                     outcome. Include only if a run could plausibly not know it.
Drop anything a competent agent already knows. If the attempts differ only by \
luck, say so instead of inventing a pattern. At most 8 bullets, one line each.

Return JSON only:
{{"verdicts": [{{"id": "<id>", "verdict": "pass"|"fail", "why": "<sentence>"}}],
  "faults":   [{{"id": "<id>", "fault": "<what went wrong>", "prevention": "<rule>"}}],
  "key_steps":[{{"id": "<id>", "step": "<what this pass did that failures didn't>"}}],
  "guidance": [{{"rule": "<one line>", "kind": "discriminative"|"procedure"|"contract",
                 "complied": ["<id>", ...], "violated": ["<id>", ...]}}],
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


TASKS = ["ab-testing", "academic-warning", "flagged-transactions", "game-statistics",
         "live-transactions", "price-comparison", "inventory-sync",
         "woocommerce-update-cover", "filter-low-selling-products",
         "woocommerce-stock-alert"]


def distil_one(task: str, job: str, level: str) -> dict:
    """One teacher call over one task's rollouts. No labels, no reward."""
    import anthropic
    prompt, truth = build(task, job, level)
    client = anthropic.Anthropic(api_key=api_key())
    with client.messages.stream(model=MODEL, max_tokens=32000,
                                output_config={"effort": "high"},
                                messages=[{"role": "user", "content": prompt}]) as st:
        msg = st.get_final_message()
    text = next((b.text for b in msg.content if b.type == "text"), "")
    data = json.loads(text[text.find("{"):text.rfind("}") + 1])
    data["_usage"] = {"in": msg.usage.input_tokens, "out": msg.usage.output_tokens}
    data["_truth"] = truth          # for scoring the judge afterwards, never shown
    return data


def cmd_loto(job: str, level: str) -> int:
    """Distil each task separately, then give each task the OTHER tasks' rules.

    Leave-one-task-out: guidance injected into task T is derived only from
    rollouts of the other nine, so an improvement cannot be T's own answer
    handed back to it.
    """
    import concurrent.futures as cf

    raw = Path("jobs/_teacher/raw")
    raw.mkdir(parents=True, exist_ok=True)
    todo = [t for t in TASKS if not (raw / f"{t}.json").exists()]
    print(f"distilling {len(todo)} tasks ({len(TASKS) - len(todo)} cached)")
    with cf.ThreadPoolExecutor(max_workers=5) as ex:
        futs = {ex.submit(distil_one, t, job, level): t for t in todo}
        for f in cf.as_completed(futs):
            t = futs[f]
            try:
                d = f.result()
            except Exception as e:                       # noqa: BLE001
                print(f"  {t}: FAILED {e!r}")
                continue
            (raw / f"{t}.json").write_text(json.dumps(d, indent=1))
            hits = sum(1 for v in d["verdicts"]
                       if (v["verdict"] == "pass") == (d["_truth"].get(v["id"]) == 1.0))
            print(f"  {t}: {len(d['guidance'])} rules, judge {hits}/{len(d['verdicts'])}, "
                  f"${d['_usage']['in']*5e-6 + d['_usage']['out']*25e-6:.2f}")

    out = Path("jobs/_teacher")
    for task in TASKS:
        rules, seen = [], set()
        for other in TASKS:
            if other == task:
                continue
            f = raw / f"{other}.json"
            if not f.exists():
                continue
            d = json.loads(f.read_text())
            for g in d.get("guidance", []):
                # a rule is worth carrying only if breaking it actually cost a
                # run; the teacher's own verdicts decide that, not the grader
                verdicts = {v["id"]: v["verdict"] for v in d["verdicts"]}
                viol = g.get("violated", [])
                if viol and all(verdicts.get(i) == "pass" for i in viol):
                    continue
                key = g["rule"][:60].lower()
                if key in seen:
                    continue
                seen.add(key)
                rules.append({"rule": g["rule"], "kind": g.get("kind", "?"),
                              "from": other, "n_violated": len(viol)})
        rules.sort(key=lambda r: (r["kind"] != "discriminative", -r["n_violated"]))
        (out / f"{task}.json").write_text(json.dumps(rules[:10], indent=1))
        print(f"  -> {task}: {len(rules[:10])} rules from {len(TASKS)-1} other tasks")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 1
    task = argv[1]
    job = argv[argv.index("--job") + 1] if "--job" in argv else None
    level = argv[argv.index("--level") + 1] if "--level" in argv else "L2"
    if task == "loto":
        return cmd_loto(job or "big-log", level)

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
    print(f"confidence: {data.get('confidence')}")

    # Audit by POLARITY, not presence. A rule cited on both sides is the
    # strongest evidence there is — passes show it followed, failures show the
    # cost of breaking it. What kills a rule is a violator that passed anyway;
    # a complier that failed is fine, since runs fail for many reasons.
    print("\nDISTILLED GUIDANCE (audited against the grader)")
    for b in data["guidance"]:
        comp, viol = b.get("complied", []), b.get("violated", [])
        got_away = [i for i in viol if truth.get(i) == 1.0]
        verdict = ("UNSOUND" if got_away else
                   "sound" if len(viol) >= 2 else "weak (few violations seen)")
        print(f"  [{b.get('kind','?'):14}] {b['rule']}")
        print(f"      complied={len(comp)} ({sum(1 for i in comp if truth.get(i)==1.0)} passed)"
              f"  violated={len(viol)} ({sum(1 for i in viol if truth.get(i)==0.0)} failed)"
              f"  -> {verdict}"
              + (f"  <-- {got_away} broke it and still passed" if got_away else ""))

    if data.get("faults"):
        print("\nPER-FAILURE FAULTS")
        for f_ in data["faults"]:
            ok = "" if truth.get(f_["id"]) == 0.0 else "  <-- grader says this one PASSED"
            print(f"  {f_['id']}: {f_['fault']}{ok}")
            print(f"      prevention: {f_['prevention']}")
    Path("jobs/_cards").mkdir(parents=True, exist_ok=True)
    out = Path("jobs/_cards") / f"_teacher_{task}.json"
    out.write_text(json.dumps({"task": task, **data, "truth": truth}, indent=1))
    print(f"\nwritten: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
