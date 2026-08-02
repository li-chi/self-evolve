#!/usr/bin/env python3
"""Oracle for git-bug-hunt.

Emails the commit author through the same tool surface the agent has
(emails MCP). The commit details ship in
/solution/groundtruth_workspace/expected_author_info.json — the agent
derives the same facts from the LUFFY repo's history in /app; the body
follows /app/template.txt exactly, as the grader requires.
"""

import json
import subprocess
import sys

with open("/solution/groundtruth_workspace/expected_author_info.json",
          encoding="utf-8") as f:
    info = json.load(f)
with open("/app/template.txt", encoding="utf-8") as f:
    template = f.read()

body = (template
        .replace("{Author's Name}", info["name"])
        .replace("{Commit Hash}", info["commit_hash"])
        .replace("{Full Commit Message}", info["commit_message"]))

out = subprocess.run(
    ["mcp-tool", "call", "emails", "send_email", json.dumps({
        "to": info["email"],
        "subject": "[URGENT] Performance Issue Investigation "
                   "Regarding Your Commit",
        "body": body,
    })],
    capture_output=True, text=True, check=True).stdout.strip()
print(f"oracle: {out}")
if "successfully" not in out:
    sys.exit(1)
