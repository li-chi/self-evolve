"""Induce a harness's conventions from the request/response stream alone.

The endpoint is not told the response format, which field carries the action,
what an action looks like, or how to find a result inside the terminal output.
All four are recoverable from the stream, and this module recovers them:

  1. response format   parse-rate over the raw completions; modal key set
  2. action field      the field whose tokens reappear in the next observation
  3. action grammar    prefix induction over action strings -> families like
                       `mcp-tool call <slot> <slot>`, giving a tool key without
                       knowing what MCP is
  4. result delimiter  the text that precedes an echoed action, learned as the
                       longest common suffix across occurrences

What stays irreducible: the action has to be observable in the next request,
echoed as text or returned as a tool result. If a harness rewrites its
observations wholesale, nothing provider-side can align to them.

    .venv/bin/python -m tools.evolve.contract            # print what it learns
"""

from __future__ import annotations

import collections
import json
import re
import sys
from dataclasses import dataclass, field
from typing import Any, Iterable

from tools.evolve import policy, replay

SLOT = "<slot>"
# A token is an identifier if it is short and word-like; anything else (a JSON
# blob, a quoted script) is a free argument and ends the grammar prefix.
IDENT = re.compile(r"[\w.\-/]{1,40}$")


@dataclass
class Contract:
    parse_rate: float = 0.0
    schema: tuple[str, ...] = ()
    action_field: str = ""
    field_echo: dict[str, float] = field(default_factory=dict)
    families: list[tuple[str, ...]] = field(default_factory=list)
    delimiter: str = ""
    n_turns: int = 0

    # ---- using what was learned -------------------------------------------

    def action_key(self, command: str) -> tuple[str, ...] | None:
        """Key an action by its family's slot values, e.g. (service, tool)."""
        for fam in self.families:
            k = _match(fam, command.strip())
            if k:
                return k
        return None

    def segment(self, observation: str) -> list[str]:
        """Split an observation into one chunk per executed action."""
        if not self.delimiter:
            return [observation]
        parts = re.split(re.escape(self.delimiter), observation)
        return parts[1:] if len(parts) > 1 else parts

    def to_dict(self) -> dict[str, Any]:
        return {
            "n_turns": self.n_turns,
            "parse_rate": round(self.parse_rate, 3),
            "schema": list(self.schema),
            "action_field": self.action_field,
            "field_echo": {k: round(v, 3) for k, v in self.field_echo.items()},
            "families": [" ".join(f) for f in self.families],
            "delimiter": self.delimiter,
        }


# --------------------------------------------------------------------------
# 1 + 2: response format and action field
# --------------------------------------------------------------------------


def _learn_schema(turns: list) -> tuple[float, tuple[str, ...], list[tuple]]:
    shapes: collections.Counter = collections.Counter()
    parsed = []
    for t in turns:
        obj = policy.parse_terminus(t.response)
        if isinstance(obj, dict) and obj:
            shapes[tuple(sorted(obj))] += 1
            parsed.append((t, obj))
    rate = len(parsed) / max(len(turns), 1)
    schema = shapes.most_common(1)[0][0] if shapes else ()
    return rate, schema, parsed


def _learn_action_field(parsed: list[tuple]) -> tuple[str, dict[str, float]]:
    """The action is whatever the environment echoes back."""
    hit: collections.Counter = collections.Counter()
    tot: collections.Counter = collections.Counter()
    for t, obj in parsed:
        if not t.observation:
            continue
        for k, v in obj.items():
            s = v if isinstance(v, str) else json.dumps(v)
            for frag in re.findall(r"[\w/.\-]{8,}", s)[:30]:
                tot[k] += 1
                hit[k] += frag in t.observation
    echo = {k: hit[k] / tot[k] for k in tot if tot[k] >= 20}
    best = max(echo, key=echo.get) if echo else ""
    return best, echo


# --------------------------------------------------------------------------
# 3: action grammar
# --------------------------------------------------------------------------


def _prefix_tokens(command: str, max_depth: int = 6) -> list[str]:
    out = []
    for tok in command.split()[:max_depth]:
        if not IDENT.match(tok):
            break
        out.append(tok)
    return out


def _induce(seqs: list[list[str]], min_support: int,
            depth: int = 0, max_depth: int = 6) -> list[str]:
    """Literal where one token dominates, `<slot>` where many compete.

    Support is re-checked at every depth: without that, a position seen a
    handful of times in one task gets promoted into the grammar and the family
    overfits to that task (`ls <slot> <slot> <slot> /app/os_hw3/Python/`).
    """
    if depth >= max_depth or len(seqs) < min_support:
        return []
    heads = collections.Counter(s[depth] for s in seqs if len(s) > depth)
    if not heads:
        return []
    total = sum(heads.values())
    top, n = heads.most_common(1)[0]
    # A closed vocabulary is a literal position even when it has several
    # values; a slot is a position whose values keep growing with the corpus.
    if n / total >= 0.8:
        deeper = [s for s in seqs if len(s) > depth and s[depth] == top]
        return [top] + _induce(deeper, min_support, depth + 1, max_depth)
    if len(heads) >= 3:
        deeper = [s for s in seqs if len(s) > depth]
        return [SLOT] + _induce(deeper, min_support, depth + 1, max_depth)
    return []


def _learn_families(commands: Iterable[str], min_support: int = 20
                    ) -> list[tuple[str, ...]]:
    """Families whose slots are a *reused vocabulary*, i.e. tool identities.

    `cat <slot>` is a real pattern but its slot is a fresh path nearly every
    time, so it names no reusable contract. `mcp-tool call <slot> <slot>` has
    slots drawn from a small set used over and over — that is a tool key.
    """
    cmds = [c.strip() for c in commands]
    by_head: dict[str, list[list[str]]] = collections.defaultdict(list)
    for c in cmds:
        toks = _prefix_tokens(c)
        if toks:
            by_head[toks[0]].append(toks)

    fams: list[tuple[tuple[str, ...], int, float]] = []
    for _, seqs in by_head.items():
        if len(seqs) < min_support:
            continue
        pat = tuple(_induce(seqs, min_support))
        if not pat or SLOT not in pat:
            continue
        keys = [k for k in (_match(pat, c) for c in cmds) if k]
        if len(keys) < min_support:
            continue
        reuse = len(set(keys)) / len(keys)   # low = categorical, high = free-form
        if reuse <= 0.5:
            fams.append((pat, len(keys), reuse))

    fams.sort(key=lambda p: (-len(p[0]), -p[1]))
    return [f for f, _, _ in fams]


def _match(pattern: tuple[str, ...], command: str) -> tuple[str, ...] | None:
    toks = command.split()
    if len(toks) < len(pattern):
        return None
    slots = []
    for pat, tok in zip(pattern, toks):
        if pat == SLOT:
            slots.append(tok)
        elif pat != tok:
            return None
    return tuple(slots) or None


# --------------------------------------------------------------------------
# 4: result delimiter
# --------------------------------------------------------------------------


def _common_suffix(strings: list[str]) -> str:
    if not strings:
        return ""
    s = strings[0]
    for other in strings[1:]:
        i = 0
        while i < len(s) and i < len(other) and s[-1 - i] == other[-1 - i]:
            i += 1
        s = s[len(s) - i:] if i else ""
        if not s:
            break
    return s


def _learn_delimiter(parsed: list[tuple], action_field: str) -> str:
    """Whatever sits immediately before an echoed action, generalised."""
    befores: list[str] = []
    for t, obj in parsed:
        if not t.observation:
            continue
        for cmd in _commands_of(obj, action_field):
            probe = cmd.strip()[:25]
            if len(probe) < 8:
                continue
            i = t.observation.find(probe)
            if i <= 0:
                continue
            line_start = t.observation.rfind("\n", 0, i) + 1
            befores.append(t.observation[line_start:i])
        if len(befores) >= 400:
            break
    # The *modal* tail, not the universal one: hostnames differ per container
    # and a single odd sample would collapse a universal common suffix to "".
    samples = [b for b in befores if b.strip()][:400]
    if not samples:
        return ""
    counts: collections.Counter = collections.Counter()
    for b in samples:
        for n in range(3, min(len(b), 24) + 1):
            counts[b[-n:]] += 1
    floor = 0.3 * len(samples)
    good = [s for s, c in counts.items() if c >= floor]
    return max(good, key=len) if good else ""


def _commands_of(obj: dict, action_field: str) -> list[str]:
    v = obj.get(action_field)
    if isinstance(v, str):
        return [v]
    if isinstance(v, list):
        out = []
        for item in v:
            if isinstance(item, str):
                out.append(item)
            elif isinstance(item, dict):
                out += [x for x in item.values() if isinstance(x, str)]
        return out
    return []


# --------------------------------------------------------------------------


def learn(turns: list) -> Contract:
    rate, schema, parsed = _learn_schema(turns)
    action_field, echo = _learn_action_field(parsed)
    commands = [c for _, obj in parsed for c in _commands_of(obj, action_field)]
    return Contract(
        parse_rate=rate,
        schema=schema,
        action_field=action_field,
        field_echo=echo,
        families=_learn_families(commands),
        delimiter=_learn_delimiter(parsed, action_field),
        n_turns=len(turns),
    )


def main(argv: list[str]) -> int:
    arms = [d.name for d in replay.STORE.iterdir() if d.is_dir()] \
        if replay.STORE.is_dir() else []
    turns = [t for arm in arms for t in replay.iter_ledger_turns(arm=arm)
             if t.role == "main"]
    if not turns:
        print("no ledger turns; run something through tools.evolve.run first")
        return 1
    c = learn(turns)
    print(json.dumps(c.to_dict(), indent=1))
    print("\naction keys induced from the grammar:")
    keyed: collections.Counter = collections.Counter()
    for t in turns:
        for cmd in t.commands:
            k = c.action_key(cmd)
            if k:
                keyed[k] += 1
    for k, n in keyed.most_common(10):
        print(f"  {n:4d}  {k}")
    print(f"  ({len(keyed)} distinct keys)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
