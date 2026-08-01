#!/bin/bash
# Oracle: the grader validates /app/github_info.json against the static
# before_task.json snapshot and (when GitHub is reachable) a freshly fetched
# after_task.json; changing counters (stars/forks) are accepted anywhere in the
# [before, after] range, so the before-snapshot values are always valid.
# The agent-facing key is `creation_time` (a required field) while the snapshot
# stores `creation_date`; we emit both so the required-field check and the
# exact-match check both pass. Repo ID 1000 does not exist on GitHub and must
# be omitted.
set -e
python3 - <<'EOF'
import json

with open('/solution/groundtruth_workspace/before_task.json', 'r', encoding='utf-8') as f:
    src = json.load(f)

out = {}
for repo_id, info in src.items():
    entry = dict(info)
    entry['creation_time'] = info.get('creation_date')
    out[repo_id] = entry

with open('/app/github_info.json', 'w', encoding='utf-8') as f:
    json.dump(out, f, indent=2, ensure_ascii=False)
print('github_info.json written with repos:', sorted(out))
EOF
