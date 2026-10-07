"""Check the chosen cascade once on the Homework 5 test labels.

Reads the proxy and threshold fixed in cascade/results/requirement.json
before the test run, and writes cascade/results/check.json. Run once; the
threshold is not changed after the result is seen.

    .venv/bin/python -m cascade.check
"""

from __future__ import annotations

import json

from cascade.run_proxy import REPO_ROOT, RESULTS_DIR, oracle, split_ids
from cascade.thresholds import cascade_at, human_labels, oracle_cost_per_verdict, score


def main() -> int:
    requirement = json.loads((RESULTS_DIR / "requirement.json").read_text())
    chosen = requirement["chosen"]
    model, threshold = chosen["proxy"], float(chosen["threshold"])
    minimum = float(requirement["agreement_with_oracle_minimum"])

    ids = split_ids("test")
    judge = oracle()
    oracle_verdicts = judge["predictions"][judge["prompt_hash"]]
    labels = human_labels()
    proxy = json.loads((RESULTS_DIR / f"proxy-{model.removeprefix('openai/')}-test.json").read_text())["results"]
    verdicts, by_proxy = cascade_at(threshold, ids, proxy, oracle_verdicts)
    [row] = score(model, "test", [threshold])

    disagree_oracle = [t for t in ids if verdicts[t] != oracle_verdicts[t]]
    disagree_human = [t for t in ids if verdicts[t] != labels[t]]
    oracle_vs_human = sum(oracle_verdicts[t] == labels[t] for t in ids) / len(ids)
    result = {
        "split": "test",
        "n": len(ids),
        "proxy": model,
        "oracle": judge["judge_id"],
        "threshold": threshold,
        "agreement_requirement": minimum,
        "agreement_with_oracle": row["agreement_with_oracle"],
        "disagreements_with_oracle": len(disagree_oracle),
        "disagreement_trace_ids": disagree_oracle,
        "meets_requirement": row["agreement_with_oracle"] >= minimum,
        "agreement_with_human_labels": row["agreement_with_human_labels"],
        "disagreements_with_human_labels": len(disagree_human),
        "oracle_agreement_with_human_labels": round(oracle_vs_human, 4),
        "proxy_share": row["proxy_share"],
        "proxy_failures_sent_to_oracle": sum("verdict" not in proxy.get(t, {}) for t in ids),
        "cost_per_1000_verdicts_usd": row["cost_per_1000_verdicts_usd"],
        "oracle_alone_cost_per_1000_verdicts_usd": round(1000 * oracle_cost_per_verdict(), 4),
        "price_note": "Proxy price is an assumed quarter of the oracle's; see optimize/config.json.",
    }
    out = RESULTS_DIR / "check.json"
    out.write_text(json.dumps(result, indent=1))
    print(json.dumps({k: v for k, v in result.items() if k != "disagreement_trace_ids"}, indent=1))
    print(f"saved {out.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
