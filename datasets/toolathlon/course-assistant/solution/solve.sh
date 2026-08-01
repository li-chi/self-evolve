#!/bin/bash
# Oracle for course-assistant: three students never submitted their NLP
# presentation, so each gets the "nlp-course-emergency" notice naming them
# and their ID. Sent through the same emails MCP server the agent uses, from
# the TA account the task is configured with. No one else may be mailed —
# the grader also checks that unrelated accounts received nothing.
set -e
python3 - <<'PY'
import json, subprocess

MISSING = [
    ("Steven Morgan", "2000016613", "smorgan@mcp.com"),
    ("Carolyn Alvarez", "2000016630", "calvarez@mcp.com"),
    ("Michelle Brooks", "2000016606", "michelle_brooks26@mcp.com"),
]

for name, sid, address in MISSING:
    body = (f"Dear {name} (Student ID: {sid}),\n\n"
            f"Our records show that your NLP course final presentation "
            f"submission has not been received. Please submit it as soon as "
            f"possible.\n\nNLP course teaching assistant")
    out = subprocess.run(
        ["mcp-tool", "call", "emails", "send_email",
         json.dumps({"to": address, "subject": "nlp-course-emergency",
                     "body": body})],
        check=True, capture_output=True, text=True).stdout
    print(f"{address}: {out.strip()}")
PY
