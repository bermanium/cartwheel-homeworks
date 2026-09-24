"""Freeze judge v2 and measure it once on the held-out test split.

This is the final HW5 measurement. Freezing is one-way per version (the
guard), and ``judge_alignment`` refuses the test split until it happens, so
the order here matters: freeze, predict, score, write the report.

Run from the repository root with ``CARTWHEEL_JUDGE_TRACE_SOURCE`` pointing at
``analysis/state/hw5_trace_inputs.json``. Without it ``load_store_traces``
falls back to Langfuse, whose per-trace endpoint is rate limited to 15/min and
will fail partway through.
"""

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT))

try:
    from dotenv import load_dotenv

    load_dotenv(REPO_ROOT / ".env")
except Exception:  # pragma: no cover - dotenv is optional
    pass

from analysis.helpers import freeze_judge, judge_alignment, run_judge  # noqa: E402

JUDGE_ID = "unsolicited_content-v2"
PROMPT_PATH = "analysis/prompts/unsolicited_content-v2.txt"


def main() -> int:
    record = freeze_judge(JUDGE_ID)
    print(f"frozen: {record['status']} at {record['frozen_at']}", flush=True)

    run_judge(JUDGE_ID, split="test", batch_size=10)
    metrics = judge_alignment(JUDGE_ID, split="test")

    report = {
        "judge_id": JUDGE_ID,
        "mode": record["mode"],
        "split": "test",
        "model": record["model"],
        "prompt_path": PROMPT_PATH,
        "prompt_hash": record["prompt_hash"],
        "frozen_at": record["frozen_at"],
        **metrics,
    }
    out = REPO_ROOT / "analysis" / "report" / f"test-{JUDGE_ID}.json"
    out.write_text(json.dumps(report, indent=2))
    print("REPORT")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
