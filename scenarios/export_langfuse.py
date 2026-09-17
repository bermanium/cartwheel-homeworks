"""Export all Langfuse traces joined to a scenario JSONL file.

Usage (after loading ``.env`` and completing the runs):

    uv run python -m scenarios.export_langfuse \
      scenarios/support_scenarios.jsonl traces/support_traces.json
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from observability.instrument import load_env
from scenarios.validate import load_jsonl, validate_scenarios


def _jsonable(value: Any) -> Any:
    """Convert an SDK response model into plain JSON data.

    The Langfuse SDK returns pydantic v1 models, whose ``dict()`` keeps
    datetime objects, so those models go through their own JSON encoder.
    """
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", by_alias=True)
    if hasattr(value, "json") and hasattr(value, "dict"):
        return json.loads(value.json(by_alias=True))
    if hasattr(value, "dict"):
        return value.dict(by_alias=True)
    if hasattr(value, "__dict__") and not isinstance(value, type):
        return dict(vars(value))
    return value


def _json_default(value: Any) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def _attribute_scenario_id(metadata: Any) -> str | None:
    """Read the scenario id from trace or observation metadata.

    Langfuse stores OpenTelemetry span attributes under ``metadata.attributes``
    (a JSON string in ClickHouse, a dict from the API), so the id is checked
    there as well as at the top level.
    """
    if not isinstance(metadata, dict):
        return None
    value = metadata.get("cartwheel.scenario_id")
    if not value:
        attributes = metadata.get("attributes")
        if isinstance(attributes, str):
            try:
                attributes = json.loads(attributes)
            except ValueError:
                attributes = None
        if isinstance(attributes, dict):
            value = attributes.get("cartwheel.scenario_id")
    return str(value) if value else None


def _scenario_id(record: Any) -> str | None:
    """Find the scenario attribute on a trace or one of its observations."""
    if isinstance(record, dict):
        found = _attribute_scenario_id(record.get("metadata"))
        if found:
            return found
        for value in record.values():
            found = _scenario_id(value)
            if found:
                return found
    elif isinstance(record, list):
        for value in record:
            found = _scenario_id(value)
            if found:
                return found
    return None


def _retrying(call: Any, *args: Any, **kwargs: Any) -> Any:
    """Call the API, honouring Langfuse Cloud's 429 retryAfterSeconds."""
    for _ in range(8):
        try:
            return call(*args, **kwargs)
        except Exception as exc:  # ApiError carries the hint in its body
            if getattr(exc, "status_code", None) != 429:
                raise
            body = getattr(exc, "body", None)
            wait = 30
            if isinstance(body, dict):
                wait = (body.get("details") or {}).get("retryAfterSeconds", 30)
            print(f"  rate limited, waiting {wait}s", flush=True)
            time.sleep(float(wait) + 1)
    raise RuntimeError("rate limited repeatedly by the Langfuse API")


def export_scenario_traces(
    scenario_ids: set[str], client: Any, *, page_size: int = 100
) -> list[dict[str, Any]]:
    """Fetch trace records whose metadata carries a selected scenario id.

    Assembled from two paged endpoints rather than one GET per trace.
    Langfuse Cloud rate limits ``GET /api/public/traces/{traceId}`` to 15
    requests per minute, so fetching a 250-scenario run one trace at a time
    fails with 429 well before it finishes.

    Traces are collected first and filtered last, because the scenario id is
    not always on the trace summary: when it is only set on a child span, the
    id has to be read from the observations.
    """
    traces: dict[str, dict[str, Any]] = {}
    page = 1
    while True:
        response = _retrying(client.api.trace.list, page=page, limit=page_size)
        batch = list(response.data or [])
        for trace_summary in batch:
            record = _jsonable(trace_summary)
            if not isinstance(record, dict) or "id" not in record:
                continue
            # trace.list returns `observations` as a list of ids; replace it
            # with the full span objects fetched below, keeping the ids so the
            # export still records what the summary claimed.
            record["observation_ids"] = record.get("observations") or []
            record["observations"] = []
            record["_summary_scenario_id"] = _attribute_scenario_id(
                getattr(trace_summary, "metadata", None)
            )
            traces[record["id"]] = record
        if len(batch) < page_size:
            break
        page += 1

    page = 1
    while True:
        response = _retrying(client.api.observations.get_many, page=page, limit=page_size)
        batch = list(response.data or [])
        for observation in batch:
            record = _jsonable(observation)
            trace = traces.get(record.get("traceId"))
            if trace is not None:
                trace["observations"].append(record)
        if len(batch) < page_size:
            break
        page += 1

    matches: list[dict[str, Any]] = []
    for record in traces.values():
        scenario_id = record.pop("_summary_scenario_id", None) or _scenario_id(record)
        if scenario_id not in scenario_ids:
            continue
        record.setdefault("cartwheel_scenario_id", scenario_id)
        matches.append(record)
    return matches


def main() -> None:
    parser = argparse.ArgumentParser(description="Export Cartwheel scenario traces from Langfuse.")
    parser.add_argument("scenarios", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument(
        "--allow-missing",
        action="store_true",
        help="write a partial export instead of failing when a scenario has no trace",
    )
    args = parser.parse_args()

    records = load_jsonl(args.scenarios)
    validate_scenarios(records)
    scenario_ids = {record["id"] for record in records}
    load_env()
    from langfuse import Langfuse

    traces = export_scenario_traces(scenario_ids, Langfuse())
    exported_ids = {trace.get("cartwheel_scenario_id") for trace in traces}
    missing = sorted(scenario_ids - exported_ids)
    if missing and not args.allow_missing:
        preview = ", ".join(missing[:10])
        raise RuntimeError(
            f"{len(missing)} scenario ids have no exported trace ({preview}); "
            "finish the runs or pass --allow-missing for a diagnostic export"
        )
    payload = {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "scenario_count": len(scenario_ids),
        "trace_count": len(traces),
        "missing_scenario_ids": missing,
        "traces": traces,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=_json_default) + "\n"
    )
    print(
        f"Exported {len(traces)} traces for {len(exported_ids)} of "
        f"{len(scenario_ids)} scenarios to {args.output}"
    )


if __name__ == "__main__":
    main()
