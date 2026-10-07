"""Score the cascade at several confidence thresholds.

For each threshold, the cascade keeps the proxy's verdict when the proxy's
confidence is at or above it and uses the oracle's saved verdict otherwise. A
proxy call that failed or returned no usable answer always goes to the oracle.

Columns (handout Part D, step 2):

- agreement_with_oracle: share of examples where cascade == oracle.
- agreement_with_human_labels: share where cascade == the Homework 5 label.
- proxy_share: share of examples the proxy decides.
- cost_per_1000_verdicts_usd: the proxy runs on every example; the oracle
  runs on the rest. Proxy cost is its measured average tokens per call at its
  price in optimize/config.json. Oracle cost is its average tokens per call as
  measured in Part A (profile/results/judge_token_sample.json) at its price,
  with no cached-input discount (the judge's prefix is rarely cached).

    .venv/bin/python -m cascade.thresholds --split development
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
from pathlib import Path

from cascade.run_proxy import ORACLE_ID, REPO_ROOT, RESULTS_DIR, SPLITS, oracle, split_ids

FIELDS = ["threshold", "agreement_with_oracle", "agreement_with_human_labels",
          "proxy_share", "cost_per_1000_verdicts_usd"]
PROXIES = ["openai/rl-muse-spark-1-1-playground", "openai/rl-muse-spark-1-2-playground"]
NEVER = 1.01  # a threshold no confidence reaches: the oracle decides everything


def prices(model: str) -> tuple[float, float]:
    table = json.loads((REPO_ROOT / "optimize" / "config.json").read_text())["prices_per_million_tokens_usd"]
    return float(table[model]["input"]), float(table[model]["output"])


def oracle_cost_per_verdict() -> float:
    sample = json.loads((REPO_ROOT / "profile" / "results" / "judge_token_sample.json").read_text())
    rows = [s["usage"] for s in sample["samples"].values() if s.get("usage")]
    input_price, output_price = prices(oracle()["model"])
    return (statistics.mean(r["input_tokens"] for r in rows) * input_price
            + statistics.mean(r["output_tokens"] for r in rows) * output_price) / 1_000_000


def proxy_cost_per_verdict(model: str, results: dict) -> float:
    rows = [r["usage"] for r in results.values() if r.get("usage")]
    input_price, output_price = prices(model)
    return (statistics.mean(r["input_tokens"] for r in rows) * input_price
            + statistics.mean(r["output_tokens"] for r in rows) * output_price) / 1_000_000


def human_labels() -> dict[str, int]:
    path = REPO_ROOT / "analysis" / "state" / "hw5_labels" / "unsolicited_content.jsonl"
    return {row["trace_id"]: int(row["label"])
            for row in map(json.loads, path.read_text().splitlines()) if row}


def cascade_at(threshold: float, ids, proxy, oracle_verdicts) -> tuple[dict[str, int], int]:
    """Trace id -> cascade verdict, and how many the proxy decided."""
    verdicts, by_proxy = {}, 0
    for tid in ids:
        row = proxy.get(tid, {})
        if "verdict" in row and row["confidence"] >= threshold:
            verdicts[tid] = row["verdict"]
            by_proxy += 1
        else:
            verdicts[tid] = oracle_verdicts[tid]
    return verdicts, by_proxy


def score(model: str, split: str, thresholds: list[float] | None = None) -> list[dict]:
    ids = split_ids(split)
    judge = oracle()
    oracle_verdicts = judge["predictions"][judge["prompt_hash"]]
    labels = human_labels()
    path = RESULTS_DIR / f"proxy-{model.removeprefix('openai/')}-{split}.json"
    proxy = json.loads(path.read_text())["results"]
    proxy_cost, oracle_cost = proxy_cost_per_verdict(model, proxy), oracle_cost_per_verdict()

    if thresholds is None:
        seen = {r["confidence"] for r in proxy.values() if "verdict" in r}
        thresholds = sorted({0.0, NEVER, *seen})
    rows = []
    for t in thresholds:
        verdicts, by_proxy = cascade_at(t, ids, proxy, oracle_verdicts)
        share = by_proxy / len(ids)
        rows.append({
            "threshold": t,
            "agreement_with_oracle": round(sum(verdicts[i] == oracle_verdicts[i] for i in ids) / len(ids), 4),
            "agreement_with_human_labels": round(sum(verdicts[i] == labels[i] for i in ids) / len(ids), 4),
            "proxy_share": round(share, 4),
            "cost_per_1000_verdicts_usd": round(1000 * (proxy_cost + (1 - share) * oracle_cost), 4),
        })
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--split", choices=sorted(SPLITS), default="development")
    args = parser.parse_args(argv)
    for model in PROXIES:
        rows = score(model, args.split)
        out = RESULTS_DIR / f"thresholds-{model.removeprefix('openai/')}-{args.split}.csv"
        with out.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(rows)
        print(f"== {model} (oracle {ORACLE_ID}) -> {out.relative_to(REPO_ROOT)}")
        for row in rows:
            print("  " + "  ".join(f"{k}={row[k]}" for k in FIELDS))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
