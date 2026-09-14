"""Read Cartwheel traces from Langfuse and shape them for the viewer.

Deliberately a different shape from ``analysis/helpers/normalization.py``.
That normalizer exists to feed Module 2's error analysis, so it flattens:
it drops observation ids, parent links, ``level`` and ``statusMessage``. The
viewer needs exactly those, because its job is to let a human see what
happened, including the parts that went wrong.

The rule here is that nothing is silently dropped or prettified away. A null
input stays null and is rendered as missing rather than as an empty string,
because "the model was called with nothing" and "the model was called with an
empty string" are different bugs.
"""

from __future__ import annotations

import json
import os
from typing import Any

from analysis.helpers.langfuse_io import LangfuseNotConfigured, is_configured
from analysis.helpers.normalization import _data, _metadata

# The span the Homework 2 endpoint opens around one request.
ROOT_SPAN = "cartwheel.session_message"

# Langfuse observation levels that mean something went wrong.
BAD_LEVELS = {"ERROR", "WARNING"}


def client() -> Any:
    """An authenticated Langfuse client, or a clear error explaining why not."""
    if not is_configured():
        raise LangfuseNotConfigured(
            "Langfuse is not configured. Set LANGFUSE_PUBLIC_KEY, "
            "LANGFUSE_SECRET_KEY and LANGFUSE_HOST in .env."
        )
    from langfuse import Langfuse

    return Langfuse()


def _plain(value: Any) -> Any:
    """Flatten an SDK enum to its string value.

    The Langfuse client returns ``ObservationLevel.ERROR`` rather than
    ``"ERROR"``. It compares equal to the string, so filtering works either
    way, but it renders as the qualified name, which is noise on screen.
    """
    if value is None:
        return None
    return getattr(value, "value", value)


def _as_json(value: Any) -> Any:
    """Decode a JSON string into structure, leaving everything else alone.

    Tool arguments and results arrive as strings often enough that showing
    them raw would mean reading escaped JSON by eye. A string that is not
    JSON is returned unchanged rather than forced.
    """
    value = _data(value)
    if isinstance(value, str):
        stripped = value.strip()
        if stripped[:1] in "{[":
            try:
                return json.loads(stripped)
            except ValueError:
                return value
    return value


def _messages_from(value: Any) -> list[dict[str, Any]]:
    """Pull OTel GenAI messages out of a span's input or output.

    The endpoint records ``[{"role": ..., "parts": [{"type": "text",
    "content": ...}]}]``. Anything that is not that shape is passed through
    as a single opaque message so it is still visible.
    """
    value = _as_json(value)
    if value is None:
        return []
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list):
        return [{"role": "unknown", "text": None, "raw": value}]

    messages = []
    for item in value:
        item = _as_json(item)
        if not isinstance(item, dict):
            messages.append({"role": "unknown", "text": None, "raw": item})
            continue
        parts = item.get("parts")
        text = None
        if isinstance(parts, list):
            chunks = [
                str(p.get("content"))
                for p in parts
                if isinstance(p, dict) and p.get("content") is not None
            ]
            text = "\n".join(chunks) if chunks else None
        elif item.get("content") is not None:
            text = str(item["content"])
        messages.append(
            {
                "role": str(item.get("role") or "unknown"),
                "text": text,
                "raw": item,
            }
        )
    return messages


def _tool_failure(result: Any) -> str | None:
    """The tool's own error code, if the result says it failed.

    Cartwheel tools return a structured dict rather than raising, so a refusal
    is a successful span carrying a failed result. A viewer that only looked
    at span status would call this a clean run.
    """
    result = _as_json(result)
    if not isinstance(result, dict):
        return None
    if result.get("ok") is False or result.get("error"):
        return str(result.get("error") or "not_ok")
    return None


def _observation(raw: Any) -> dict[str, Any]:
    """One observation, keeping the identity and failure fields intact."""
    obs = _data(raw)
    attributes = (obs.get("metadata") or {}).get("attributes") or {}
    if isinstance(attributes, str):
        try:
            attributes = json.loads(attributes)
        except ValueError:
            attributes = {}

    kind = _plain(obs.get("type"))
    output = obs.get("output")
    tool_error = _tool_failure(output) if kind == "TOOL" else None
    level = _plain(obs.get("level"))
    status_message = obs.get("statusMessage")
    if status_message is not None and not isinstance(status_message, str):
        # Some SDK errors arrive as a dict; keep the content, make it readable.
        status_message = json.dumps(status_message, default=str)

    return {
        # Identity and structure. call_id is what the brief asks to preserve;
        # parent_id is what lets the UI show the tree.
        "call_id": obs.get("id"),
        "parent_id": obs.get("parentObservationId"),
        "type": kind,
        "name": obs.get("name"),
        # Timestamps stay as they came, including a null end time on a span
        # that never closed.
        "start_time": obs.get("startTime"),
        "end_time": obs.get("endTime"),
        "latency_seconds": obs.get("latency"),
        # Payloads. None means absent, and the UI says so.
        "input": _as_json(obs.get("input")),
        "output": _as_json(output),
        "model": obs.get("model"),
        "usage": _data(obs.get("usageDetails")) or None,
        "total_tokens": obs.get("totalTokens"),
        "attributes": attributes,
        # Failure, from three independent sources.
        "level": level,
        "status_message": status_message,
        "tool_error": tool_error,
        "failed": bool(tool_error) or level in BAD_LEVELS or bool(status_message),
        # Everything, for the raw view. Nothing in the UI is unexplainable.
        "raw": obs,
    }


def _elapsed_ms(start: Any, base: Any) -> float | None:
    """Milliseconds from the start of the request to this step."""
    if start is None or base is None:
        return None
    try:
        return round((start - base).total_seconds() * 1000, 1)
    except TypeError:
        return None


def _journey(root: dict[str, Any] | None, observations: list[dict]) -> list[dict]:
    """The run as an ordered sequence of what the agent actually did.

    Question, then every model call and tool call interleaved in the order
    they happened, then the answer. The model calls are included on purpose:
    without them the loop looks like "ask, tool, answer" when what really
    happened is "think, act, think again, answer", and the thinking is where
    almost all the wall-clock time goes.
    """
    steps: list[dict[str, Any]] = []
    base = root["start_time"] if root else None
    if base is None:
        starts = [o["start_time"] for o in observations if o["start_time"]]
        base = min(starts) if starts else None

    def add(**step: Any) -> None:
        step["index"] = len(steps)
        steps.append(step)

    if root:
        for message in _messages_from(root["input"]):
            add(
                kind="user",
                label="asked",
                text=message["text"],
                raw=message["raw"],
                elapsed_ms=0.0,
                latency_seconds=None,
                failed=False,
            )

    for obs in observations:
        if obs["type"] == "TOOL":
            add(
                kind="tool",
                label=obs["name"],
                call_id=obs["call_id"],
                arguments=obs["input"],
                result=obs["output"],
                start_time=obs["start_time"],
                end_time=obs["end_time"],
                latency_seconds=obs["latency_seconds"],
                elapsed_ms=_elapsed_ms(obs["start_time"], base),
                tool_error=obs["tool_error"],
                level=obs["level"],
                status_message=obs["status_message"],
                failed=obs["failed"],
            )
        elif obs["type"] == "GENERATION":
            seen = _messages_from(obs["input"])
            add(
                kind="model",
                label=obs["model"] or obs["name"],
                call_id=obs["call_id"],
                start_time=obs["start_time"],
                end_time=obs["end_time"],
                latency_seconds=obs["latency_seconds"],
                elapsed_ms=_elapsed_ms(obs["start_time"], base),
                messages_seen=len(seen),
                prompt_messages=seen,
                # Null for this model API; shown as not recorded rather than
                # as an empty answer, and not counted as a failure.
                output=obs["output"],
                total_tokens=obs["total_tokens"],
                usage=obs["usage"],
                level=obs["level"],
                status_message=obs["status_message"],
                failed=obs["failed"],
            )

    if root:
        replies = _messages_from(root["output"])
        if not replies:
            # A run that raised never set gen_ai.output.messages. Say that,
            # rather than ending the journey silently.
            add(
                kind="missing",
                label="no reply",
                text=None,
                note="no final reply recorded; the run did not complete",
                elapsed_ms=None,
                latency_seconds=None,
                failed=True,
            )
        for message in replies:
            add(
                kind="assistant",
                label="answered",
                text=message["text"],
                raw=message["raw"],
                elapsed_ms=None,
                latency_seconds=None,
                failed=False,
            )
    return steps


def summarize(trace: Any) -> dict[str, Any]:
    """One row for the trace list."""
    raw = _data(trace)
    metadata = _metadata(raw)
    html_path = raw.get("htmlPath")
    host = os.environ.get("LANGFUSE_HOST", "").rstrip("/")
    # Langfuse promotes the root span's gen_ai.input.messages to trace level,
    # so the list can show what was asked without fetching every observation.
    asked = next(
        (m["text"] for m in _messages_from(raw.get("input")) if m.get("text")), None
    )
    return {
        "permalink": f"{host}{html_path}" if host and html_path else None,
        "preview": asked,
        "trace_id": raw.get("id"),
        "name": raw.get("name"),
        "timestamp": raw.get("timestamp"),
        "latency_seconds": raw.get("latency"),
        "session_id": raw.get("sessionId"),
        "user_role": metadata.get("cartwheel.user_role"),
        "user_id": metadata.get("cartwheel.user_id"),
        "prompt_version": metadata.get("cartwheel.prompt_version"),
        "scenario_id": metadata.get("cartwheel.scenario_id"),
        "html_path": raw.get("htmlPath"),
    }


def detail(trace: Any) -> dict[str, Any]:
    """One trace, fully expanded, with every problem found already flagged."""
    raw = _data(trace)
    observations = [_observation(o) for o in raw.get("observations") or []]
    observations.sort(key=lambda o: (o["start_time"] is None, o["start_time"]))

    roots = [o for o in observations if o["name"] == ROOT_SPAN]
    root = roots[0] if roots else None

    problems: list[dict[str, str]] = []
    if root is None:
        problems.append(
            {
                "kind": "missing_root",
                "detail": (
                    f"no {ROOT_SPAN} span; the request may still be running, "
                    "or the root span never closed"
                ),
            }
        )
    elif root["output"] is None:
        problems.append(
            {"kind": "no_final_reply", "detail": "the root span has no output"}
        )
    for obs in observations:
        if obs["tool_error"]:
            problems.append(
                {
                    "kind": "tool_error",
                    "detail": f"{obs['name']} returned {obs['tool_error']}",
                    "call_id": obs["call_id"],
                }
            )
        if obs["level"] in BAD_LEVELS:
            problems.append(
                {
                    "kind": "span_level",
                    "detail": f"{obs['name']} is {obs['level']}",
                    "call_id": obs["call_id"],
                }
            )
        if obs["status_message"]:
            problems.append(
                {
                    "kind": "status_message",
                    "detail": f"{obs['name']}: {obs['status_message']}",
                    "call_id": obs["call_id"],
                }
            )

    tools = [o["name"] for o in observations if o["type"] == "TOOL"]
    return {
        **summarize(raw),
        "attributes": (root or {}).get("attributes") or _metadata(raw),
        "journey": _journey(root, observations),
        "observations": observations,
        "problems": problems,
        "span_count": len(observations),
        "tool_count": len(tools),
        # Distinct names, first-use order, for the tool filter in the list.
        "tools": list(dict.fromkeys(tools)),
        "raw": raw,
    }


def list_traces(limit: int = 50) -> list[dict[str, Any]]:
    """Trace summaries, newest first. One API call, no observation fetches."""
    lf = client()
    response = lf.api.trace.list(page=1, limit=limit)
    return [summarize(t) for t in response.data or []]


def get_trace(trace_id: str) -> dict[str, Any]:
    """One full trace, including every observation."""
    return detail(client().api.trace.get(trace_id))
