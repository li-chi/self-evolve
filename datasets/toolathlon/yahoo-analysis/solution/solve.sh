#!/bin/bash
# Oracle for yahoo-analysis. The grader recomputes the answer from live
# yfinance data, so the reference solution computes it the same way (the
# computation is vendored in gt_calc.py) and fills results_template.md,
# then renames it to results.md as the task requires.
set -e
cd /app
python3 - <<'PY'
import sys
from datetime import date
from dateutil.relativedelta import relativedelta

sys.path.insert(0, "/solution")
from gt_calc import get_gt

stats = {t: get_gt(t) for t in ("NVDA", "AAPL")}

rows = []
for ticker in ("NVDA", "AAPL"):
    for h in ("4m", "5m", "6m"):
        s = stats[ticker][h]
        rows.append(
            f"| {ticker:6s} | {h[0]} months  | {s['Hit Rate (%)']:<12} | "
            f"{s['Avg Excess Return (%)']:<21} | {s['#Signals']:<8} | "
            f"{s['Fails']:<9} |")

# "More reliable" = higher mean hit rate across horizons.
mean_hit = {t: sum(stats[t][h]["Hit Rate (%)"] for h in ("4m", "5m", "6m")) / 3
            for t in stats}
choice = max(mean_hit, key=mean_hit.get)

end = date.today()
start = end - relativedelta(years=2)

md = f"""# Analyst Rating Accuracy Table for NVDA and AAPL

## Table

| Ticker | Horizon   | Hit Rate (%) | Avg Excess Return (%) | #Signals | #Excluded |
|--------|-----------|--------------|-----------------------|----------|-----------|
""" + "\n".join(rows) + f"""

## More Reliable
Choice: {choice}
Conclusion: {choice} has the higher average hit rate across the 4, 5 and 6
month horizons ({mean_hit[choice]:.2f}% vs
{mean_hit['AAPL' if choice == 'NVDA' else 'NVDA']:.2f}%), so its analyst
ratings were the more reliable signal over the period.

## Data Range
Start: {start.isoformat()}
End: {end.isoformat()}
"""
open("/app/results.md", "w").write(md)
import os
if os.path.exists("/app/results_template.md"):
    os.remove("/app/results_template.md")
print("oracle: results.md written")
PY
