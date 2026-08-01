#!/bin/bash
# Oracle: build /app/os_hw3/{C,Rust,Python} from the groundtruth
# expected_mapping.json (name/college/id rename + language classification),
# then delete the original code files as the task requests.
set -e

python3 - <<'EOF'
import json, os, shutil

APP = "/app"
mapping = json.load(open("/solution/groundtruth_workspace/expected_mapping.json", encoding="utf-8"))

base = os.path.join(APP, "os_hw3")
for sub in ("C", "Rust", "Python"):
    os.makedirs(os.path.join(base, sub), exist_ok=True)

for student, info in mapping["expected_students"].items():
    src = os.path.join(APP, info["original_file"])
    dst = os.path.join(base, info["target_folder"], info["expected_renamed"])
    shutil.copy2(src, dst)

# Task says to delete all original code files afterwards.
for f in os.listdir(APP):
    p = os.path.join(APP, f)
    if os.path.isfile(p) and f.endswith((".c", ".rs", ".py")):
        os.remove(p)

print("organized", mapping["total_files"], "files into os_hw3/")
EOF
