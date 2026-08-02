"""Mine tool-contract cards from rollouts already on disk. No GPU, no labels.

The unit is an **error → corrected invocation** pair observed inside one
rollout: the agent calls a tool, the terminal shows an error, and a later call
to the same tool succeeds. Ground truth is the terminal output itself, so this
needs neither a reward signal nor extra sampling — which matters, because the
endpoint we are modelling has neither.

What gets learned is a property of the *environment*, not of the model:

    bigquery_run_query: the JSON argument is wrapped in single quotes for the
    shell, so a single-quoted SQL literal ends the shell string. Use \\"...\\".

That stays true for any model driving the same mock servers.

Source matters for the claim. The default is the hook's ledger: the raw
request and the raw completion, with the command parsed out by us, exactly
what a model endpoint sees. `--source trajectory` reads Harbor's
`trajectory.json` instead, which is a harness artifact — the commands there
are Harbor's own parse, including cases where its parser auto-corrected
malformed output that an endpoint would have had to handle itself. Use it only
to bootstrap from rollouts that predate the hook, and say so.

Nothing here reads `verifier/reward.txt`, the grader, or `result.json`. The
learning signal is the observation the harness sent back on the next turn.

    .venv/bin/python -m tools.evolve.mine cards            # from the ledger
    .venv/bin/python -m tools.evolve.mine cards --source trajectory
    .venv/bin/python -m tools.evolve.mine cards --json cards.json
    .venv/bin/python -m tools.evolve.mine score            # leave-one-out
"""

from __future__ import annotations

import collections
import difflib
import json
import re
import sys
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator

from tools.evolve import contract, replay

ERROR_LINE = re.compile(
    r"^.*(?:[Ee]rror|ERROR|Traceback|not found|not in allowed|[Ii]nvalid|"
    r"no such|failed|Failed|denied).*$",
    re.M,
)

# Normalise an error into a class: drop the specific identifiers so the same
# mistake made against different tables/ids collapses to one card.
_NORMALISERS = (
    (re.compile(r"'[^']*'"), "'<X>'"),
    (re.compile(r'"[^"]*"'), '"<X>"'),
    (re.compile(r"`[^`]*`"), "`<X>`"),
    (re.compile(r"\b\d+\b"), "<N>"),
    (re.compile(r"\s+"), " "),
)


# `Traceback (most recent call last):` is the header, not the error; the
# informative line is the exception at the end of the block.
GENERIC = ("traceback (most recent call last):",)


def _errors(text: str) -> list[str]:
    lines = [e.strip() for e in ERROR_LINE.findall(text)]
    specific = [e for e in lines if e.lower() not in GENERIC]
    return specific or lines


def error_class(line: str) -> str:
    s = line.strip()
    for pat, repl in _NORMALISERS:
        s = pat.sub(repl, s)
    return s[:120]


@dataclass
class Invocation:
    task: str
    run: str
    turn: int
    key: tuple[str, ...]      # induced by contract.py, e.g. (call, svc, tool)
    args: str
    errors: list[str]

    @property
    def name(self) -> str:
        return ".".join(self.key)


@dataclass
class Pair:
    key: tuple[str, ...]
    error: str
    error_cls: str
    failed_args: str
    fixed_args: str
    task: str
    run: str


def card_kind(failed: str, fixed: str, error: str) -> str:
    """How transferable a card's lesson is.

    arg-name  the argument *keys* changed -> a schema fact about the tool,
              true wherever that tool is used
    syntax    the payload never parsed -> an encoding rule (shell quoting,
              JSON escaping), also tool-wide
    value     same keys, different value -> the lesson is which table/bucket
              exists in *that task*. Under a high-compliance consumer this is
              worse than nothing: it hands one task's identifiers to another.
    """
    if "argument JSON is invalid" in error or "unrecognized arguments" in error:
        return "syntax"
    try:
        a, b = json.loads(failed), json.loads(fixed)
    except (json.JSONDecodeError, TypeError):
        return "syntax"
    if not isinstance(a, dict) or not isinstance(b, dict):
        return "value"
    return "arg-name" if set(a) != set(b) else "value"


TRANSFERABLE = ("arg-name", "syntax")


@dataclass
class Card:
    key: tuple[str, ...]
    error_cls: str
    kind: str = "value"
    failures: int = 0
    rollouts: set[tuple[str, str]] = field(default_factory=set)
    tasks: set[str] = field(default_factory=set)
    example_failed: str = ""
    example_fixed: str = ""
    example_error: str = ""

    @property
    def name(self) -> str:
        return ".".join(self.key)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": list(self.key),
            "name": self.name,
            "kind": self.kind,
            "error_cls": self.error_cls,
            "failures": self.failures,
            "rollouts": len(self.rollouts),
            "tasks": sorted(self.tasks),
            "example_error": self.example_error,
            "example_failed": self.example_failed,
            "example_fixed": self.example_fixed,
        }


def invocations(turn, ct: contract.Contract) -> Iterator[Invocation]:
    """Actions in one turn, with errors attributed per action.

    Both the action grammar and the delimiter used to split the output come
    from `contract.py`, which induced them from the stream — nothing here
    knows that MCP or a shell prompt exist.

    The pane is a fixed-width capture, so a long command wraps and its
    arguments cannot be read back out of the echo; the key survives wrapping,
    which is all the matching needs, and the arguments come from
    `turn.commands`, which is verbatim.
    """
    parsed = [(c, ct.action_parts(c)) for c in turn.commands]
    parsed = [(c, p) for c, p in parsed if p]
    if not parsed:
        return

    chunks = ct.segment(turn.observation)
    for cmd, (key, args) in parsed:
        if len(parsed) == 1:
            errors = _errors(turn.observation)
        else:
            for ch in chunks:
                if all(tok in ch[:400] for tok in key):
                    errors = _errors(ch)
                    break
            else:
                continue  # cannot attribute; skip rather than guess
        yield Invocation(task=turn.task, run=turn.run, turn=turn.turn,
                         key=key, args=args, errors=[e.strip() for e in errors])


def mine_pairs(turns: Iterable[Any], ct: contract.Contract | None = None
               ) -> list[Pair]:
    """error → later success on the same action key, within one rollout."""
    turns = list(turns)
    ct = ct or contract.learn(turns)
    by_rollout: dict[tuple[str, str], list[Invocation]] = collections.defaultdict(list)
    for t in turns:
        for inv in invocations(t, ct):
            by_rollout[(inv.task, inv.run)].append(inv)

    pairs: list[Pair] = []
    for (task, run), invs in by_rollout.items():
        pending: dict[tuple[str, str], Invocation] = {}
        for inv in sorted(invs, key=lambda i: i.turn):
            if inv.errors:
                pending[inv.key] = inv        # newest failure wins
            elif inv.key in pending:
                bad = pending.pop(inv.key)
                if bad.args == inv.args:      # identical retry proves nothing
                    continue
                # A correction is a small edit to the same action. A wholly
                # different payload is the agent moving on to different work,
                # which teaches nothing about the tool's contract.
                if difflib.SequenceMatcher(None, bad.args, inv.args).ratio() < 0.5:
                    continue
                if bad.errors[0].strip().lower() in GENERIC:
                    continue
                pairs.append(Pair(
                    key=inv.key,
                    error=bad.errors[0], error_cls=error_class(bad.errors[0]),
                    failed_args=bad.args, fixed_args=inv.args,
                    task=task, run=run,
                ))
    return pairs


# An action key made of flags, redirections or heredoc markers (`-c`, `>`,
# `<<`, `'PYEOF'`) names no tool. Those families fire on nearly every inline
# script and their "fix" is an unrelated script, so a card built on them is
# noise that a high-fire-rate consumer like the guard arm will amplify.
TOOLISH = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{2,}$")


def names_a_tool(key: tuple[str, ...]) -> bool:
    return bool(key) and all(TOOLISH.match(t) for t in key)


def build_cards(pairs: list[Pair]) -> list[Card]:
    cards: dict[tuple[tuple[str, ...], str], Card] = {}
    for p in pairs:
        if not names_a_tool(p.key):
            continue
        k = (p.key, p.error_cls)
        c = cards.get(k)
        if c is None:
            c = cards[k] = Card(p.key, p.error_cls,
                                kind=card_kind(p.failed_args, p.fixed_args, p.error),
                                example_error=p.error[:200],
                                example_failed=p.failed_args[:400],
                                example_fixed=p.fixed_args[:400])
        c.failures += 1
        c.rollouts.add((p.task, p.run))
        c.tasks.add(p.task)
    # a card seen in one rollout only may be an accident of that rollout
    return sorted(cards.values(), key=lambda c: (-len(c.rollouts), -c.failures))


def score(pairs: list[Pair]) -> None:
    """Leave-one-rollout-out: would a store built from the others cover this?

    Coverage is an upper bound on what retrieval can do — it says the lesson
    was learnable from other rollouts, not that injecting it would help.
    """
    by_roll: dict[tuple[str, str], list[Pair]] = collections.defaultdict(list)
    for p in pairs:
        by_roll[(p.task, p.run)].append(p)

    hit = miss = 0
    cross_task_hit = 0
    for held, held_pairs in by_roll.items():
        others = [p for k, v in by_roll.items() if k != held for p in v]
        known = {(p.key, p.error_cls) for p in others}
        known_other_task = {
            (p.key, p.error_cls) for p in others if p.task != held[0]
        }
        for p in held_pairs:
            k = (p.key, p.error_cls)
            if k in known:
                hit += 1
                if k in known_other_task:
                    cross_task_hit += 1
            else:
                miss += 1
    tot = hit + miss
    print(f"leave-one-rollout-out over {len(by_roll)} rollouts, {tot} error->fix pairs")
    print(f"  covered by other rollouts        : {hit:4d} ({100*hit/max(tot,1):.0f}%)")
    print(f"  ...and by a *different task* too : {cross_task_hit:4d} "
          f"({100*cross_task_hit/max(tot,1):.0f}%)   <- the transfer that matters")
    print(f"  novel to their rollout           : {miss:4d} ({100*miss/max(tot,1):.0f}%)")


# Rollouts produced *under* an intervention are not acquisition data: mining
# them feeds the store its own influence back. `ab-json`/`ab-xml` are someone
# else's parser experiment, so their serving config is unknown to us.
INTERVENED = {"big-guard", "cardab-cards", "ab-parse", "ab-json", "ab-xml"}


def turn_source(argv: list[str]) -> Iterator[Any]:
    """Endpoint-visible turns by default; the harness artifact only on request."""
    src = argv[argv.index("--source") + 1] if "--source" in argv else "ledger"
    if src == "trajectory":
        skip = set(INTERVENED)
        if "--exclude" in argv:
            skip |= set(argv[argv.index("--exclude") + 1].split(","))
        print(f"# source: Harbor trajectory.json, excluding {sorted(skip)}")
        return replay.iter_trajectory_turns(exclude_jobs=skip)
    arms = [d.name for d in replay.STORE.iterdir() if d.is_dir()] \
        if replay.STORE.is_dir() else []
    print(f"# source: hook ledger, arms={arms or 'none'}")
    return (t for arm in arms for t in replay.iter_ledger_turns(arm=arm))


def as_note(card_dicts: list[dict], budget: int = 1400) -> str:
    """Render cards as the text injected after the latest observation."""
    lines = ["[operator note] Contracts observed in this environment on earlier "
             "runs. They are facts about these tools, not suggestions:"]
    for c in card_dicts:
        lines.append(f"- {c['name']}: calling it as {c['example_failed'][:130]} "
                     f"failed with \"{c['example_error'][:90]}\"; "
                     f"the form that works is {c['example_fixed'][:130]}")
        if sum(len(x) for x in lines) > budget:
            break
    return "\n".join(lines)


def write_store(pairs: list[Pair], out: str, min_rollouts: int = 2) -> None:
    """One card file per task, built from every *other* task's rollouts.

    Leave-one-task-out, so injecting a card into a task can never be that
    task's own lesson played back — what it measures is transfer.
    """
    import os
    os.makedirs(out, exist_ok=True)
    tasks = sorted({p.task for p in pairs})
    for task in tasks:
        others = [p for p in pairs if p.task != task]
        cards = [c for c in build_cards(others)
                 if len(c.rollouts) >= min_rollouts and c.kind in TRANSFERABLE]
        with open(os.path.join(out, f"{task}.json"), "w") as f:
            json.dump([c.to_dict() for c in cards], f, indent=1)
    print(f"{len(tasks)} leave-one-task-out card files -> {out}/")
    for task in tasks:
        n = len(json.load(open(os.path.join(out, f"{task}.json"))))
        print(f"  {task:34} {n:3d} cards from other tasks")


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "cards"
    pairs = mine_pairs(turn_source(argv))
    if cmd == "score":
        score(pairs)
        return 0
    if cmd not in ("cards", "store"):
        print(__doc__)
        return 1

    if cmd == "store":
        write_store(pairs, argv[argv.index("--out") + 1])
        return 0

    cards = build_cards(pairs)
    if "--json" in argv:
        out = argv[argv.index("--json") + 1]
        with open(out, "w") as f:
            json.dump([c.to_dict() for c in cards], f, indent=1)
        print(f"{len(cards)} cards -> {out}")
        return 0

    print(f"{len(pairs)} error->fix pairs -> {len(cards)} cards "
          f"across {len({c.key for c in cards})} actions\n")
    for c in cards[:20]:
        print(f"[{len(c.rollouts):2d} rollouts, {c.failures:2d} failures] "
              f"{c.name}")
        print(f"    error : {c.error_cls}")
        print(f"    failed: {c.example_failed[:150]}")
        print(f"    fixed : {c.example_fixed[:150]}")
        print(f"    tasks : {', '.join(sorted(c.tasks))}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
