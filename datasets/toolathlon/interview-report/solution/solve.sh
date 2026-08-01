#!/bin/bash
# Oracle: the grader (evaluation/check_local.py) checks 13
# Interview_Assessment_Report_<Name>.docx files in /app (text content +
# table cell shading vs groundtruth) and a /app/recommend.txt containing
# exactly "John Smith". The groundtruth's strict_hiring_analysis.py /
# strict_hiring_groundtruth.txt are grader-side aids, not graded outputs.
set -e
cp /solution/groundtruth_workspace/Interview_Assessment_Report_*.docx /app/
printf 'John Smith\n' > /app/recommend.txt
