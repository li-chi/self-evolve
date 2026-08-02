#!/usr/bin/env python3
"""Oracle for apply-phd-email.

Does what the task asks through the same tool surface the agent has: zip
the application materials in the required structure and email them to the
requester (kaiming's request, receiver mailbox myersj@mcp.com) with the
exact required subject.

The materials tree ships in /solution/groundtruth_workspace/files.tar.gz;
the agent would assemble the same tree from the files in /app following
the inbox email's instructions.
"""

import json
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

WORK = Path("/tmp/oracle-phd")
WORK.mkdir(parents=True, exist_ok=True)

with tarfile.open("/solution/groundtruth_workspace/files.tar.gz") as t:
    t.extractall(WORK)

materials = next(p for p in WORK.iterdir()
                 if p.name.startswith("Application_Materials"))
zip_path = Path("/app") / f"{materials.name}.zip"
with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
    for f in sorted(materials.rglob("*")):
        z.write(f, f.relative_to(WORK))

student_id = materials.name.rsplit("_", 1)[-1]
out = subprocess.run(
    ["mcp-tool", "call", "emails", "send_email", json.dumps({
        "to": "myersj@mcp.com",
        "subject": f"PhD Application Materials Submission "
                   f"(Student ID: {student_id})",
        "body": "Dear Professor,\n\nPlease find attached my application "
                "materials as requested.\n\nBest regards,\nMary Castillo\n",
        "attachments": [zip_path.name],
    })],
    capture_output=True, text=True, check=True).stdout.strip()
print(f"oracle: {out}")
if "successfully" not in out:
    sys.exit(1)
