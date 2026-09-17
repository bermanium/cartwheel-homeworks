"""Scripted scenario runner. Instructor-provided and complete.

Plays each scenario from a JSONL file against the running endpoint
(server/app.py), 1 to 25 turns, sequentially. Sequential execution is fine at
course scale; a full simulated-user loop is deferred to Module 3's sandbox.

Each turn sends the scenario id along, so the server stamps
`cartwheel.scenario_id` on the trace and Module 3 can join traces back to
ground truth.

Usage:
    uv run uvicorn server.app:app --port 8010          # in one terminal
    uv run python -m scenarios.runner path/to/scenarios.jsonl \
        --model gpt-5.5 [--base-url http://localhost:8010] [--limit 50] \
        [--output scenarios/final-primary.jsonl] [--resume] [--ids a,b]

Without ``--output``, results land in the gitignored scratch directory
``scenarios/results/``. Homework submissions should name an output explicitly.

Reruns: ``--resume`` keeps the completed records already in ``--output`` and
runs only the scenarios whose record is missing or has another status.
``--ids`` runs only the named scenarios and replaces their records, keeping
the rest. A rerun does not reset the database, so check the order state of a
scenario that refunds or cancels before rerunning it.
"""

from __future__ import annotations

import argparse
import json
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from scenarios.validate import MAX_SCENARIO_TURNS, load_jsonl, validate_scenarios

REPO_ROOT = Path(__file__).resolve().parents[1]
RESULTS_DIR = REPO_ROOT / "scenarios" / "results"

# Demo users per role (seeded by seed/generate.py). A scenario tuple may
# name an explicit user_id instead.
DEFAULT_USERS = {"shopper": 1, "merchant": 9001, "support": 9501}
MAX_TURNS_PER_SCENARIO = MAX_SCENARIO_TURNS
# Raised from the shipped 180 s. A single agent turn measured 146 s against the
# Meta model endpoint, and longer under congestion, so 180 s recorded completed
# turns as errors while the server returned 200 OK (HW2 saw the same).
REQUEST_TIMEOUT_S = 900


def _post(url: str, payload: dict[str, Any], token: str | None = None) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_S) as response:
        return json.loads(response.read())


def load_scenarios(path: Path) -> list[dict[str, Any]]:
    scenarios = load_jsonl(path)
    validate_scenarios(scenarios)
    return scenarios


def run_scenario(
    scenario: dict[str, Any], base_url: str, model: str | None
) -> dict[str, Any]:
    """Play one scenario end to end. Returns a result record."""
    tuple_ = scenario.get("tuple", {})
    role = tuple_.get("role", "shopper")
    user_id = tuple_.get("user_id", DEFAULT_USERS.get(role, 1))
    turns: list[dict[str, str]] = []
    started = time.time()
    try:
        session = _post(f"{base_url}/sessions", {"user_id": user_id, "role": role})
        messages = [scenario["opening_message"]]
        followups = scenario.get("followups") or []
        # Every followup is an exact user utterance. The scenario skill forbids
        # persona notes or generation instructions in this field.
        messages.extend(followups[: MAX_TURNS_PER_SCENARIO - 1])
        for message in messages:
            reply = _post(
                f"{base_url}/sessions/{session['session_id']}/messages",
                {
                    "message": message,
                    "model": model,
                    "scenario_id": scenario["id"],
                },
                token=session["token"],
            )
            turns.append({"user": message, "agent": reply["reply"]})
        status = "completed"
        error = None
    except (urllib.error.URLError, urllib.error.HTTPError, KeyError, TimeoutError) as exc:
        status = "error"
        error = str(exc)
    return {
        "scenario_id": scenario["id"],
        "scenario_group": scenario["scenario_group"],
        "model": model,
        "status": status,
        "error": error,
        "turns": turns,
        "expected": scenario["expected"],
        "duration_s": round(time.time() - started, 2),
    }


def load_results(path: Path) -> dict[str, dict[str, Any]]:
    """Existing result records keyed by scenario id (empty when absent)."""
    if not path.exists():
        return {}
    records = load_jsonl(path)
    return {record["scenario_id"]: record for record in records}


def plan_run(
    scenarios: list[dict[str, Any]],
    existing: dict[str, dict[str, Any]],
    ids: set[str] | None = None,
    resume: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split into (scenarios to run, existing records to keep).

    ``ids`` restricts the run to the named scenarios. ``resume`` skips
    scenarios whose existing record has status ``completed``. Records for
    scenarios that will run again are dropped so the rerun replaces them.
    """
    known = {scenario["id"] for scenario in scenarios}
    if ids is not None:
        unknown = sorted(ids - known)
        if unknown:
            raise SystemExit(f"unknown scenario ids: {', '.join(unknown)}")
    to_run = []
    for scenario in scenarios:
        if ids is not None and scenario["id"] not in ids:
            continue
        record = existing.get(scenario["id"])
        if resume and record is not None and record.get("status") == "completed":
            continue
        to_run.append(scenario)
    rerun_ids = {scenario["id"] for scenario in to_run}
    kept = [record for sid, record in existing.items() if sid not in rerun_ids]
    return to_run, kept


def main() -> None:
    parser = argparse.ArgumentParser(description="Run scenarios against the endpoint.")
    parser.add_argument("scenarios", type=Path, help="scenario JSONL file")
    parser.add_argument("--base-url", default="http://localhost:8010")
    parser.add_argument("--limit", type=int, default=None, help="run only the first N")
    parser.add_argument("--model", required=True, help="model recorded on every request and result")
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="explicit result JSONL path (recommended for committed homework artifacts)",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="keep completed records in --output and run only missing or failed scenarios",
    )
    parser.add_argument(
        "--ids",
        default=None,
        help="comma separated scenario ids to run; their records replace earlier ones",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=1,
        help=(
            "scenarios to play at once. Default 1 keeps the shipped sequential "
            "behaviour. Higher values are safe only when state-changing "
            "scenarios target distinct records, since parallel refunds or "
            "cancellations would otherwise invalidate each other's expected "
            "outcome."
        ),
    )
    args = parser.parse_args()

    scenarios = load_scenarios(args.scenarios)
    out_path = args.output or RESULTS_DIR / f"run-{int(time.time())}.jsonl"
    ids = {item.strip() for item in args.ids.split(",") if item.strip()} if args.ids else None
    merging = args.resume or ids is not None
    existing = load_results(out_path) if merging else {}
    to_run, kept = plan_run(scenarios, existing, ids=ids, resume=args.resume)
    if args.limit is not None:
        to_run = to_run[: args.limit]
    if merging:
        print(f"Keeping {len(kept)} existing records; running {len(to_run)} scenarios.")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    completed = 0
    done = 0
    write_lock = threading.Lock()
    with open(out_path, "w") as out:
        for record in kept:
            out.write(json.dumps(record) + "\n")
        out.flush()

        def record_result(result: dict[str, Any]) -> None:
            """Append one result. Locked, because threads share the handle."""
            nonlocal completed, done
            with write_lock:
                out.write(json.dumps(result) + "\n")
                out.flush()
                done += 1
                completed += result["status"] == "completed"
                print(f"[{done}/{len(to_run)}] {result['scenario_id']}: {result['status']}")

        if args.concurrency > 1:
            with ThreadPoolExecutor(args.concurrency) as pool:
                futures = [
                    pool.submit(run_scenario, scenario, args.base_url, args.model)
                    for scenario in to_run
                ]
                for future in as_completed(futures):
                    record_result(future.result())
        else:
            for scenario in to_run:
                record_result(run_scenario(scenario, args.base_url, args.model))
    print(f"\n{completed}/{len(to_run)} completed. Results: {out_path}")
    print("Now open Langfuse and run reports/smoke.sql against ClickHouse.")


if __name__ == "__main__":
    main()
