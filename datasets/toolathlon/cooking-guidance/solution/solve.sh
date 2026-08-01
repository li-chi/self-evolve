#!/bin/bash
# Oracle: upstream has no per-file groundtruth for the agent outputs (the GT
# workspace only carries the recipe corpus the grader reads). The files in
# /solution/oracle_outputs/ were computed offline with the grader's own logic
# (dish combo with 87.5% ingredient coverage + full shopping list) and verified
# to pass the upstream evaluation.
set -e
cp /solution/oracle_outputs/cuisine.json /app/cuisine.json
cp /solution/oracle_outputs/shopping.csv /app/shopping.csv
echo "oracle outputs installed"
