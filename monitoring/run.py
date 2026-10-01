"""Run the Homework 7 monitor for one period or for the last N hours.

    python -m monitoring.run --period before [--dry-run] [--background]
    python -m monitoring.run --last-hours 24

One run: fetch the window's traces from Langfuse, build one conversation
record per session, sample it (random + risk groups), judge the union once
with the frozen Homework 5 judge, correct the random-sample rate for the
judge's measured error, write scores back to Langfuse, and record the period
in ``monitoring/history.jsonl``.

A comparison period (``--period``) must contain every scenario in
``scenarios/monitoring_scenarios.jsonl`` on the configured model. When a
scenario was retried, only the session its results file recorded is kept; the
other sessions are set aside and listed, never merged into the conversation.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
MONITORING_DIR = REPO_ROOT / "monitoring"
CONFIG_PATH = MONITORING_DIR / "config.json"
HISTORY_PATH = MONITORING_DIR / "history.jsonl"
CHART_PATH = MONITORING_DIR / "prevalence.svg"
OUTPUT_DIR = MONITORING_DIR / "output"
SCENARIOS_PATH = REPO_ROOT / "scenarios" / "monitoring_scenarios.jsonl"

# Judge calls are slow and the endpoint drops connections, so the sample is
# judged one conversation at a time and each verdict is cached as it arrives.
# Observations of a trace that starts just before the window's end can start
# after it; fetch a little past the end so those conversations stay complete.
OBSERVATION_SLACK = timedelta(minutes=30)


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------


def _retrying(call: Any, **kwargs: Any) -> Any:
    """Call a Langfuse list endpoint, waiting out 429s and dropped reads."""
    for attempt in range(8):
        try:
            return call(**kwargs)
        except Exception as exc:
            if getattr(exc, "status_code", None) == 429:
                body = getattr(exc, "body", None)
                wait = 30
                if isinstance(body, dict):
                    wait = (body.get("details") or {}).get("retryAfterSeconds", 30)
                print(f"  Langfuse rate limit, waiting {wait}s", flush=True)
                time.sleep(float(wait) + 1)
            elif isinstance(exc, httpx.TransportError):
                print(f"  Langfuse read failed ({type(exc).__name__}), retrying", flush=True)
                time.sleep(10 * (attempt + 1))
            else:
                raise
    raise RuntimeError("Langfuse kept failing; try again later")


def _paged(call: Any, page_size: int, **kwargs: Any) -> list[Any]:
    items: list[Any] = []
    page = 1
    while True:
        batch = list(_retrying(call, page=page, limit=page_size, **kwargs).data or [])
        items.extend(batch)
        if len(batch) < page_size:
            return items
        page += 1


def fetch_window(start: datetime, end: datetime) -> list[dict[str, Any]]:
    """Normalized traces that started in [start, end), observations attached.

    Two paged endpoints instead of one GET per trace: Langfuse Cloud limits
    the per-trace endpoint to 15 requests a minute.
    """
    from langfuse import Langfuse

    from analysis.helpers.normalization import _data, normalize_trace

    client = Langfuse(timeout=180)
    summaries = _paged(
        client.api.trace.list,
        25,
        from_timestamp=start,
        to_timestamp=end,
        fields="core,io",
    )
    raw: dict[str, dict[str, Any]] = {}
    for summary in summaries:
        record = _data(summary)
        record["observations"] = []
        raw[record["id"]] = record
    observations = _paged(
        client.api.observations.get_many,
        100,
        from_start_time=start,
        to_start_time=end + OBSERVATION_SLACK,
    )
    for observation in observations:
        record = _data(observation)
        trace = raw.get(record.get("traceId"))
        if trace is not None:
            trace["observations"].append(record)
    return [normalize_trace(record) for record in raw.values()]


# ---------------------------------------------------------------------------
# Conversations
# ---------------------------------------------------------------------------


def _merge_session(traces: list[dict[str, Any]]) -> dict[str, Any]:
    """One session's traces as the single record ``build_conversation`` expects."""
    ordered = sorted(traces, key=lambda t: t.get("timestamp") or "")
    merged = dict(ordered[-1])
    merged["observations"] = [o for t in ordered for o in t["observations"]]
    merged["trace"] = [m for t in ordered for m in t["trace"]]
    return merged


def _judge_text(merged: dict[str, Any], record_id: str) -> str:
    """The normalized conversation text exactly as the Homework 5 judge read it.

    Same builder and flattener as ``analysis/run_judges.py::prepare_inputs``:
    ordered turns, the agent's reasoning, each tool call and its result, and the
    final reply. No system prompt, labels, notes, or scenario metadata.
    """
    from analysis.helpers.normalization import normalize_trace
    from analysis.run_judges import _assert_no_leakage, _messages_for

    messages = _messages_for(merged)
    if not messages:
        raise ValueError(f"conversation {record_id} renders no messages")
    _assert_no_leakage([{"trace_id": record_id, "trace": messages}])
    return normalize_trace({"trace_id": record_id, "trace": messages})["text"]


def build_conversation_record(traces: list[dict[str, Any]]) -> dict[str, Any]:
    """One conversation record: the final trace id, tool evidence, turns, text."""
    ordered = sorted(traces, key=lambda t: t.get("timestamp") or "")
    merged = _merge_session(ordered)
    record_id = ordered[-1]["id"]
    tools = sorted(
        {
            str(o["name"])
            for t in ordered
            for o in t["observations"]
            if o.get("type") == "TOOL" and o.get("name")
        }
    )
    return {
        "id": record_id,
        "session_id": ordered[0]["meta"].get("session_id"),
        "scenario_id": ordered[0]["meta"].get("scenario_id"),
        "trace_ids": [t["id"] for t in ordered],
        "models": sorted({m for t in ordered for m in t["models"]}),
        "tools": tools,
        # One trace per user message: the server opens a trace per message.
        "turn_count": len(ordered),
        "timestamp": ordered[-1].get("timestamp"),
        "text": _judge_text(merged, record_id),
    }


def _sessions(traces: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    by_session: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for trace in traces:
        session = trace["meta"].get("session_id")
        if session:
            by_session[str(session)].append(trace)
    return by_session


def _check_models(sessions: dict[str, list[dict[str, Any]]], model: str) -> None:
    wrong = {
        trace["id"]: trace["models"]
        for traces in sessions.values()
        for trace in traces
        if any(m != model for m in trace["models"])
    }
    if wrong:
        sample = dict(list(wrong.items())[:3])
        raise SystemExit(
            f"Period rejected: {len(wrong)} eligible traces used a model other than "
            f"{model}, e.g. {sample}"
        )


def _recorded_sessions(results_path: str | None) -> dict[str, str]:
    """Scenario id -> the session its results file recorded, when known."""
    if not results_path:
        return {}
    recorded = {}
    for line in (REPO_ROOT / results_path).read_text().splitlines():
        if line.strip():
            row = json.loads(line)
            if row.get("session_id") and row.get("status") == "completed":
                recorded[row["scenario_id"]] = row["session_id"]
    return recorded


def comparison_conversations(
    traces: list[dict[str, Any]], model: str, results_path: str | None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    """The 50 scenario conversations of a comparison period.

    Returns (conversations, set-aside sessions, eligible trace count). Rejects
    the period when a scenario is missing or an eligible trace ran on another
    model.
    """
    scenario_ids = [
        json.loads(line)["id"]
        for line in SCENARIOS_PATH.read_text().splitlines()
        if line.strip()
    ]
    wanted = set(scenario_ids)
    eligible = [t for t in traces if t["meta"].get("scenario_id") in wanted]
    sessions = _sessions(eligible)
    _check_models(sessions, model)

    by_scenario: dict[str, list[str]] = defaultdict(list)
    for session, session_traces in sessions.items():
        by_scenario[str(session_traces[0]["meta"]["scenario_id"])].append(session)
    missing = sorted(wanted - set(by_scenario))
    if missing:
        raise SystemExit(
            f"Period rejected: {len(missing)} of {len(wanted)} scenarios have no "
            f"trace in the window: {', '.join(missing[:10])}"
        )

    recorded = _recorded_sessions(results_path)
    conversations, set_aside = [], []
    for scenario in scenario_ids:
        candidates = sorted(
            by_scenario[scenario],
            key=lambda s: min(t.get("timestamp") or "" for t in sessions[s]),
        )
        if scenario in recorded:
            keep = recorded[scenario]
            if keep not in candidates:
                raise SystemExit(
                    f"Period rejected: the recorded session for {scenario} is not "
                    "in the window"
                )
        else:
            # A resumed run reruns only failures, so the recorded attempt is
            # always the scenario's last session.
            keep = candidates[-1]
        conversations.append(build_conversation_record(sessions[keep]))
        for session in candidates:
            if session != keep:
                set_aside.append(
                    {
                        "scenario_id": scenario,
                        "session_id": session,
                        "trace_ids": [t["id"] for t in sessions[session]],
                        "started": min(t.get("timestamp") or "" for t in sessions[session]),
                    }
                )
    return conversations, set_aside, len(eligible)


def window_conversations(
    traces: list[dict[str, Any]], model: str
) -> tuple[list[dict[str, Any]], int]:
    """Every session in a rolling window, grouped by ``meta.session_id``."""
    sessions = _sessions(traces)
    _check_models(sessions, model)
    eligible = sum(len(v) for v in sessions.values())
    return [build_conversation_record(v) for v in sessions.values()], eligible


# ---------------------------------------------------------------------------
# Judging and recording
# ---------------------------------------------------------------------------


def _judge_with_cache(
    judge_id: str,
    records: list[dict[str, Any]],
    cache_path: Path,
    background_jobs: Path | None = None,
) -> dict[str, int]:
    """Judge each record once, caching every verdict the moment it lands.

    A failed call does not stop the others; the run stops afterwards, before
    any scoring, and a rerun judges only the records still missing. With
    ``background_jobs`` the same requests are queued in background mode
    (``monitoring/background_judge.py``) instead of held open.
    """
    from monitoring.run_judges import judge_sample

    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    verdicts = cache.setdefault(judge_id, {})
    todo = [r for r in records if r["id"] not in verdicts]
    if len(todo) < len(records):
        print(f"  {len(records) - len(todo)} verdicts reused from {cache_path.name}")
    def results() -> Any:
        if background_jobs is not None:
            from monitoring.background_judge import judge_background

            yield from judge_background(judge_id, todo, background_jobs)
            return
        for record in todo:
            try:
                yield record["id"], judge_sample(judge_id, [record])[record["id"]]
            except Exception as exc:
                yield record["id"], exc

    failed = []
    for done, (trace_id, outcome) in enumerate(results(), start=1):
        if isinstance(outcome, Exception):
            failed.append(trace_id)
            print(f"  judge failed on {trace_id} ({type(outcome).__name__}: {str(outcome)[:120]})", flush=True)
            continue
        verdict = {trace_id: outcome}
        verdicts.update(verdict)
        # Another period's run may share the cache; merge into the file as it
        # is now rather than overwrite the verdicts that run has saved since.
        on_disk = json.loads(cache_path.read_text()) if cache_path.exists() else {}
        on_disk.setdefault(judge_id, {}).update(verdict)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(on_disk, indent=1))
        print(f"  judged {done}/{len(todo)}", flush=True)
    if failed:
        raise RuntimeError(
            f"{len(failed)} of {len(todo)} judge calls failed; rerun to judge only those"
        )
    return {r["id"]: int(verdicts[r["id"]]) for r in records}


def _write_history(entry: dict[str, Any]) -> list[dict[str, Any]]:
    """Replace this period's line in history.jsonl, keep the others in order."""
    rows = []
    if HISTORY_PATH.exists():
        rows = [json.loads(l) for l in HISTORY_PATH.read_text().splitlines() if l.strip()]
    rows = [r for r in rows if r.get("period") != entry["period"]] + [entry]
    order = {p["label"]: i for i, p in enumerate(load_config().get("periods", []))}
    rows.sort(key=lambda r: (order.get(r["period"], len(order)), r.get("window_from", "")))
    HISTORY_PATH.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return rows


def load_config() -> dict[str, Any]:
    return json.loads(CONFIG_PATH.read_text())


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    which = parser.add_mutually_exclusive_group(required=True)
    which.add_argument("--period", help="a period label from monitoring/config.json")
    which.add_argument("--last-hours", type=float, help="monitor a rolling window")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="stop after showing the sample size and the number of judge calls",
    )
    parser.add_argument(
        "--background",
        action="store_true",
        help="queue judge calls in background mode (for proxies that cut long calls)",
    )
    args = parser.parse_args(argv)

    try:
        from dotenv import load_dotenv

        load_dotenv(REPO_ROOT / ".env")
    except ImportError:  # pragma: no cover - CI sets the variables directly
        pass

    from monitoring.sample import DEFAULT_RISK_GROUPS, select_traces

    config = load_config()
    model = config["model"]
    if args.period:
        period = next((p for p in config["periods"] if p["label"] == args.period), None)
        if period is None or not period.get("from") or not period.get("to"):
            raise SystemExit(f"period {args.period!r} has no time range in {CONFIG_PATH.name}")
        label = period["label"]
        start, end = _parse_time(period["from"]), _parse_time(period["to"])
    else:
        end = datetime.now(timezone.utc).replace(microsecond=0)
        start = end - timedelta(hours=args.last_hours)
        label = f"last-{args.last_hours:g}h-{end.strftime('%Y-%m-%dT%H%MZ')}"
    print(f"Period {label}: {start.isoformat()} to {end.isoformat()}")

    traces = fetch_window(start, end)
    set_aside: list[dict[str, Any]] = []
    if args.period:
        conversations, set_aside, eligible = comparison_conversations(
            traces, model, period.get("results")
        )
    else:
        conversations, eligible = window_conversations(traces, model)
    print(
        f"Langfuse traces in window: {len(traces)}; eligible: {eligible}; "
        f"conversations: {len(conversations)}"
    )
    for item in set_aside:
        print(
            f"  set aside: {item['scenario_id']} session {item['session_id'][:8]} "
            f"({len(item['trace_ids'])} trace(s), started {item['started'][:19]})"
        )

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    summary: dict[str, Any] = {
        "period": label,
        "window_from": start.isoformat(),
        "window_to": end.isoformat(),
        "judge_id": config["judge_id"],
        "model": model,
        "langfuse_traces": len(traces),
        "eligible_traces": eligible,
        "conversations": len(conversations),
        "set_aside_sessions": set_aside,
    }
    if not conversations:
        summary.update({"random_sample": 0, "risk_sample": 0, "judge_calls": 0})
        (OUTPUT_DIR / f"{label}.json").write_text(json.dumps(summary, indent=1))
        print("No eligible conversations in the window; nothing to judge.")
        return 0

    groups = {name: DEFAULT_RISK_GROUPS[name] for name in config["risk_groups"]}
    plan = select_traces(conversations, config["random_rate"], groups, seed=7)
    risk_ids = list(dict.fromkeys(r["id"] for g in plan["risk_groups"].values() for r in g))
    print(f"Random sample: {len(plan['random'])} conversations")
    for name, members in plan["risk_groups"].items():
        print(f"Risk group {name}: {len(members)} conversations")
    print(
        f"Risk groups combined: {len(risk_ids)}; in both selections: "
        f"{len({r['id'] for r in plan['random']} & set(risk_ids))}"
    )
    print(f"Judge calls ({config['judge_id']}): {len(plan['to_judge'])}")
    if args.dry_run:
        print("Dry run: stopping before the judge.")
        return 0

    verdicts = _judge_with_cache(
        config["judge_id"],
        plan["to_judge"],
        OUTPUT_DIR / "verdict-cache.json",
        OUTPUT_DIR / f"background-jobs-{label}.json" if args.background else None,
    )
    random_verdicts = {r["id"]: verdicts[r["id"]] for r in plan["random"]}
    risk_verdicts = {rid: verdicts[rid] for rid in risk_ids}

    from monitoring.correct import corrected_mode_prevalence
    from monitoring.run_judges import judge_test_data
    from monitoring.write_scores import build_score_records, post_scores

    test_labels, test_preds = judge_test_data(config["judge_id"])
    estimate = corrected_mode_prevalence(list(random_verdicts.values()), test_labels, test_preds)
    mode = config["judge_mode"]
    records = build_score_records(mode, random_verdicts, risk_verdicts, estimate, label)
    # Date each verdict at its conversation rather than at the upload, so the
    # dashboard's time axis shows which period a verdict belongs to.
    conversation_time = {c["id"]: c.get("timestamp") for c in conversations}
    for record in records:
        if record["trace_id"] is not None:
            record["timestamp"] = conversation_time.get(record["trace_id"])
    written = post_scores(records)
    print(f"Scores written to Langfuse: {written}")

    summary.update(
        {
            "random_sample": len(random_verdicts),
            "risk_sample": len(risk_verdicts),
            "risk_groups": {n: [r["id"] for r in m] for n, m in plan["risk_groups"].items()},
            "judge_calls": len(plan["to_judge"]),
            "random_verdicts": random_verdicts,
            "risk_verdicts": risk_verdicts,
            "estimate": estimate,
        }
    )
    (OUTPUT_DIR / f"{label}.json").write_text(json.dumps(summary, indent=1))

    if args.period:
        history = _write_history(
            {
                "period": label,
                "judge_id": config["judge_id"],
                "model": model,
                "window_from": start.isoformat(),
                "window_to": end.isoformat(),
                "langfuse_traces": len(traces),
                "eligible_traces": eligible,
                "conversations": len(conversations),
                "random_sample": len(random_verdicts),
                "risk_sample": len(risk_verdicts),
                "set_aside_sessions": len(set_aside),
                "raw_rate": estimate["raw"],
                "corrected_rate": estimate["corrected"],
                "ci_low": estimate["ci_low"],
                "ci_high": estimate["ci_high"],
                "confidence": estimate["confidence"],
                "failure_sensitivity": estimate["failure_sensitivity"],
                "pass_specificity": estimate["pass_specificity"],
                "risk_flag_rate": round(sum(risk_verdicts.values()) / len(risk_verdicts), 4)
                if risk_verdicts
                else None,
            }
        )
        from monitoring.chart import prevalence_chart

        points = [
            {
                "label": r["period"],
                "corrected": r["corrected_rate"],
                "ci_low": r["ci_low"],
                "ci_high": r["ci_high"],
            }
            for r in history
        ]
        CHART_PATH.write_text(prevalence_chart(points, config["threshold"], mode))
    print(
        f"Raw {estimate['raw']}, corrected {estimate['corrected']} "
        f"({estimate['ci_low']}-{estimate['ci_high']}), threshold {config['threshold']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
