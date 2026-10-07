"""Build profile/results/frontier.csv from the saved development runs.

One row for the Homework 8 final run and one for each Homework 9 run. The
status uses the Homework 8 rule (`optimize.workflow.mark_dominated`): a
configuration is dominated when another has an equal or higher score and an
equal or lower cost, and is better on at least one of the two.

Cost basis. Only the Part C runs were billed with the cached-input discount;
the Homework 8 final run did not record cached tokens, so it cannot be
discounted after the fact. To compare like with like, `cost_per_100_
conversations_usd` here bills every input token at the full input price,
recomputed from each run's token counts. The discounted cost, where the
cached tokens are known, is printed alongside for the write-up.

    .venv/bin/python profile/scripts/build_frontier.py
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from optimize.workflow import mark_dominated  # noqa: E402

RESULTS = REPO_ROOT / "optimize" / "results"
RUNS = {
    "final": "development-final-openai_rl-muse-spark-1-3-sglang-playground-20261005-190657.json",
    "fewer-tokens": "development-fewer-tokens-openai_rl-muse-spark-1-3-sglang-playground-20261006-155127.json",
    "cache-before": "development-cache-before-openai_rl-muse-spark-1-3-sglang-playground-20261007-101522.json",
    "cache-after": "development-cache-after-openai_rl-muse-spark-1-3-sglang-playground-20261007-102404.json",
}
OUT_PATH = REPO_ROOT / "profile" / "results" / "frontier.csv"
FIELDS = ["candidate", "model", "development_score", "write_pass_5",
          "cost_per_100_conversations_usd", "median_latency_seconds", "frontier_status"]


def per_100(run: dict, cached_price: float | None) -> float | None:
    prices = json.loads((REPO_ROOT / "optimize" / "config.json").read_text())
    p = prices["prices_per_million_tokens_usd"][run["model"]]
    cached = run.get("cached_input_tokens")
    if cached_price is not None and cached is None:
        return None
    billed_cached = cached if cached_price is not None else 0
    cost = ((run["input_tokens"] - billed_cached) * p["input"]
            + billed_cached * (cached_price or 0.0)
            + run["output_tokens"] * p["output"]) / 1_000_000
    return round(100 * cost / run["evaluated_case_runs"], 6)


def main() -> int:
    prices = json.loads((REPO_ROOT / "optimize" / "config.json").read_text())["prices_per_million_tokens_usd"]
    runs = {name: json.loads((RESULTS / file).read_text()) for name, file in RUNS.items()}
    configurations = [
        {
            "configuration": name,
            "score": run["score"],
            "cost_per_100_conversations_usd": per_100(run, None),
            "run": run,
        }
        for name, run in runs.items()
    ]
    rows = []
    for c in mark_dominated(configurations):
        run = c["run"]
        rows.append({
            "candidate": c["configuration"],
            "model": run["model"],
            "development_score": run["score"],
            "write_pass_5": "" if run["write_pass_5"] is None else run["write_pass_5"],
            "cost_per_100_conversations_usd": c["cost_per_100_conversations_usd"],
            "median_latency_seconds": run["median_latency_seconds"],
            "frontier_status": "dominated" if c["dominated"] else "frontier",
        })
    with OUT_PATH.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    cached_price = prices[runs["final"]["model"]].get("cached_input")
    discounted = [
        {"configuration": n, "score": r["score"], "cost_per_100_conversations_usd": per_100(r, cached_price)}
        for n, r in runs.items() if per_100(r, cached_price) is not None
    ]
    status_discounted = {c["configuration"]: c["dominated"] for c in mark_dominated(discounted)}
    for row in rows:
        name = row["candidate"]
        d = per_100(runs[name], cached_price)
        shown = "n/a (no cached count)" if d is None else f"${d:.4f}"
        alt = "" if name not in status_discounted else ("dominated" if status_discounted[name] else "frontier")
        print(f"{name:<13} score={row['development_score']} full-price=${row['cost_per_100_conversations_usd']:.4f} "
              f"{row['frontier_status']:<9} | discounted={shown} {alt}  latency={row['median_latency_seconds']}s")
    print(f"wrote {OUT_PATH.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
