#!/bin/bash
# Oracle: the groundtruth desensitized documents ship in the solution dir
# (only uploaded by the oracle agent, never present for real agents).
set -e
cd /app
rm -rf desensitized_documents
tar xzf /solution/gt_files.tar.gz desensitized_documents
