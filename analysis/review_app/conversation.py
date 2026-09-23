"""Rebuild a Cartwheel trace as one flat, chronological conversation.

This module exists because neither Langfuse's trace view nor the shared
``analysis.helpers.normalization`` produces something a human can open-code at
speed. Both lose the agent's reasoning, and Langfuse additionally shows the
conclusion before the evidence.

What the raw trace actually looks like
--------------------------------------
Every ``openai.response`` GENERATION span records what was *sent to* the model
and never what came *back*: ``output`` is null on all of them. A model reply
therefore only survives as an ``assistant`` message inside the **next**
generation's ``input``. The inputs are cumulative, so:

    the last generation's input == the entire conversation so far

including, for a multi-turn session, the earlier turns. Confirmed on
``support-0043``: turn 1's last generation carries 22 messages, turn 2's
carries 30, spanning both user turns.

That single span is the source this module reads. The only thing missing from
it is the final assistant reply, which was never fed back into a later call;
that comes from the normalizer's ``trace`` field.

Why not reuse ``normalization._messages``
-----------------------------------------
It reads GENERATION ``output``, which is null here, so every intermediate
``assistant`` message is dropped. A trace rendered from it shows ten tool calls
with no statement of why any of them happened. The system prompt requires the
agent to explain its reasoning in plain text before every tool call, so that
omission hides a directly checkable requirement.

Message content is nested at ``input[i]["parts"][j]["content"]``, not at
``content``. Reading ``content`` returns ``None`` for every message and renders
an empty interface with no error, so ``_parts`` is the only accessor used here.
"""

from __future__ import annotations

import ast
import json
from typing import Any

# Tools whose results deserve their own rendering rather than a JSON blob.
# `search_help_center` returns the retrieval set that RESP-1 citations must be
# checked against, so the policy ids have to be scannable without parsing JSON.
RETRIEVAL_TOOLS = {"search_help_center"}
POLICY_TOOLS = {"get_policy"}

# Tools that change state. RESP-2 ("do not claim an action succeeded before the
# tool reports success") can only be violated where one of these appears, so the
# interface flags them.
WRITE_TOOLS = {"issue_refund", "cancel_order", "escalate_to_human"}


def _parts(message: Any) -> list[dict[str, Any]]:
    """Return a message's ``parts`` list, tolerating a malformed record."""
    if not isinstance(message, dict):
        return []
    parts = message.get("parts")
    return [p for p in parts if isinstance(p, dict)] if isinstance(parts, list) else []


def _text_of(message: Any) -> str:
    """Join the text parts of one message."""
    out = [
        str(p.get("content"))
        for p in _parts(message)
        if p.get("type") in (None, "text") and p.get("content")
    ]
    return "\n".join(out).strip()


def _loads(value: Any) -> Any:
    """Parse a tool payload that may be JSON, a Python repr, or already parsed.

    The message history stores tool results as Python ``repr`` strings
    (``{'ok': True, ...}``) rather than JSON, so ``json.loads`` fails on them.
    The TOOL spans carry proper structures and are preferred; this is only the
    fallback for when span and history cannot be paired.
    """
    if not isinstance(value, str):
        return value
    for parse in (json.loads, ast.literal_eval):
        try:
            return parse(value)
        except (ValueError, SyntaxError):
            continue
    return value


def _generations(record: dict[str, Any]) -> list[dict[str, Any]]:
    return sorted(
        (o for o in record.get("observations") or [] if o.get("type") == "GENERATION"),
        key=lambda o: str(o.get("start_time") or ""),
    )


def _tool_spans(record: dict[str, Any]) -> list[dict[str, Any]]:
    return sorted(
        (o for o in record.get("observations") or [] if o.get("type") == "TOOL"),
        key=lambda o: str(o.get("start_time") or ""),
    )


def _classify(name: str, result: Any) -> dict[str, Any]:
    """Summarize a tool result into the fields the interface renders.

    Returns the tool kind (which decides the rendering), whether the call
    succeeded, and a one-line summary for the collapsed header.
    """
    kind = "generic"
    if name in RETRIEVAL_TOOLS:
        kind = "retrieval"
    elif name in POLICY_TOOLS:
        kind = "policy"

    ok, error, summary = None, None, ""
    if isinstance(result, dict):
        ok = result.get("ok")
        error = result.get("error")
        if kind == "retrieval":
            hits = result.get("results") or []
            summary = f"{len(hits)} policies: " + ", ".join(
                str(h.get("policy_id")) for h in hits[:4] if isinstance(h, dict)
            )
        elif kind == "policy":
            summary = str(result.get("policy_id") or "")
        elif "orders" in result:
            orders = result.get("orders") or []
            summary = f"{len(orders)} orders"
        elif "order" in result and isinstance(result["order"], dict):
            o = result["order"]
            summary = f"order {o.get('order_id')} · {o.get('status')} · ${o.get('total_usd')}"
        elif "products" in result:
            summary = f"{len(result.get('products') or [])} products"
        elif error:
            summary = str(error)
        if ok is False and not summary:
            summary = str(result.get("reason") or error or "failed")
    elif isinstance(result, str):
        # Tool execution raised; the harness substitutes a plain string.
        ok = False
        summary = result[:120]

    return {"kind": kind, "ok": ok, "error": error, "summary": summary}


def build_conversation(record: dict[str, Any]) -> dict[str, Any]:
    """Return one trace as ``{system_prompt, turns:[{user, steps, reply}]}``.

    ``record`` is a trace normalized by ``analysis.helpers.normalization``; a
    multi-turn session has already been merged into a single record, so its
    observations span every turn in timestamp order.

    Each step is one lap of the model/tool loop: the agent's plain-text
    reasoning plus the tool call it introduced plus that call's result. Steps
    are numbered within their turn so an annotation can name one ("the failure
    starts at step 4").
    """
    gens = _generations(record)
    history = gens[-1].get("input") if gens else None
    if not isinstance(history, list):
        return _fallback(record)

    # The k-th tool call in the history is the k-th TOOL span in time order:
    # the agent runs one sequential loop, and the names were verified to match
    # in order. Spans carry structured results; the history carries reprs.
    spans = _tool_spans(record)

    system_prompt = ""
    turns: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    call_index = 0

    def open_turn(user_text: str) -> dict[str, Any]:
        turn = {"user": user_text, "steps": [], "reply": ""}
        turns.append(turn)
        return turn

    for message in history:
        if not isinstance(message, dict):
            continue
        role = message.get("role")

        if role == "system":
            system_prompt = system_prompt or _text_of(message)
            continue

        if role == "user":
            current = open_turn(_text_of(message))
            continue

        if role == "assistant":
            if current is None:
                current = open_turn("")
            reasoning = _text_of(message)
            calls = [p for p in _parts(message) if p.get("type") == "tool_call"]
            if not calls:
                # An assistant message with no tool call ends its turn. Only
                # the final turn's reply is absent from the history, because it
                # was never fed back into a later call.
                current["reply"] = reasoning
                continue
            for call in calls:
                name = str(call.get("name") or "")
                span = spans[call_index] if call_index < len(spans) else None
                call_index += 1
                # A missing span leaves the result None; the `tool` branch
                # below fills it from the history repr as a fallback.
                result = span.get("output") if span else None
                current["steps"].append(
                    {
                        "n": len(current["steps"]) + 1,
                        "reasoning": reasoning,
                        "tool": name,
                        "is_write": name in WRITE_TOOLS,
                        "arguments": call.get("arguments"),
                        "call_id": call.get("id"),
                        "result": result,
                        "latency": (span or {}).get("latency_seconds"),
                        "permission_denied": _denied(span),
                    }
                )
                reasoning = ""  # reasoning belongs to the first call only
            continue

        if role == "tool":
            # Fallback only: fill a result the TOOL spans did not supply.
            for part in _parts(message):
                if part.get("type") != "tool_call_response":
                    continue
                target = _step_by_call_id(turns, part.get("id"))
                if target is not None and target.get("result") is None:
                    target["result"] = _loads(part.get("response"))

    if turns and not turns[-1]["reply"]:
        turns[-1]["reply"] = _final_reply(record)

    for turn in turns:
        for step in turn["steps"]:
            step.update(_classify(step["tool"], step["result"]))

    return {
        "trace_id": record.get("id"),
        "system_prompt": system_prompt,
        "turns": turns,
    }


def _denied(span: dict[str, Any] | None) -> bool:
    """Read ``cartwheel.permission_denied`` off a TOOL span (stored as a string)."""
    if not span:
        return False
    attrs = (span.get("metadata") or {}).get("attributes") or {}
    return str(attrs.get("cartwheel.permission_denied", "")).lower() == "true"


def _step_by_call_id(turns: list[dict[str, Any]], call_id: Any) -> dict[str, Any] | None:
    if not call_id:
        return None
    for turn in turns:
        for step in turn["steps"]:
            if step.get("call_id") == call_id:
                return step
    return None


def _final_reply(record: dict[str, Any]) -> str:
    """Return the last assistant message the normalizer recovered.

    The final reply never re-enters a generation input, so the history cannot
    supply it. ``normalization`` builds it from the trace-level ``output``, and
    for a merged multi-turn record its ``trace`` list holds one reply per turn,
    the last of which is the one still missing here.
    """
    for message in reversed(record.get("trace") or []):
        if message.get("role") == "assistant" and message.get("text"):
            return str(message["text"])
    return ""


def _fallback(record: dict[str, Any]) -> dict[str, Any]:
    """Render from the normalizer's messages when no generation span exists.

    Not reached by any of the 285 Module 1 traces, which all carry generations.
    It keeps a trace from a different source renderable rather than blank.
    """
    turn: dict[str, Any] = {"user": "", "steps": [], "reply": ""}
    turns = [turn]
    for message in record.get("trace") or []:
        role = message.get("role")
        if role == "user":
            if turn["user"] or turn["steps"]:
                turn = {"user": "", "steps": [], "reply": ""}
                turns.append(turn)
            turn["user"] = str(message.get("text") or "")
        elif role == "tool_call":
            turn["steps"].append(
                {
                    "n": len(turn["steps"]) + 1,
                    "reasoning": "",
                    "tool": str(message.get("name") or ""),
                    "is_write": str(message.get("name") or "") in WRITE_TOOLS,
                    "arguments": message.get("arguments"),
                    "call_id": None,
                    "result": None,
                    "latency": None,
                    "permission_denied": False,
                }
            )
        elif role == "tool_result" and turn["steps"]:
            turn["steps"][-1]["result"] = _loads(message.get("content"))
        elif role == "assistant":
            turn["reply"] = str(message.get("text") or "")
    for t in turns:
        for step in t["steps"]:
            step.update(_classify(step["tool"], step["result"]))
    return {
        "trace_id": record.get("id"),
        "system_prompt": "",
        "turns": [t for t in turns if t["user"] or t["steps"] or t["reply"]],
    }


def outlier_flags(
    record: dict[str, Any], stats: dict[str, dict[str, float]]
) -> list[str]:
    """Return header badges for the dimensions where this trace is extreme.

    Phase 2 of the skill: structural outliers belong in the header as compact
    badges, never inline, and **most traces should carry none**. Three rules
    keep the badge rare enough to mean something:

    - Only the top decile is flagged. A short trace is not an anomaly, and
      flagging both tails put a badge on nearly half the batch.
    - ``tokens`` is never flagged. Langfuse records zero tokens for every
      Cartwheel span, so ``normalization`` falls back to a word count; a badge
      reading "76 tokens" would state a number the trace store does not hold.
    - Zero tool calls gets its own badge rather than a percentile. The system
      prompt tells the agent to prefer a tool lookup over memory, so a reply
      built without one is worth noticing on sight; 20 of the 285 traces
      answer with no lookup at all.
    """
    features = record.get("features") or {}
    flags = []

    if features.get("tool_call_count") == 0:
        flags.append("no tool calls")

    for key, label in (("tool_call_count", "tool calls"), ("turn_count", "turns")):
        value = features.get(key)
        bounds = stats.get(key)
        if value is None or not bounds or value == 0:
            continue
        if value >= bounds["p90"] and value > bounds["median"]:
            flags.append(f"{value} {label} (top 10%)")
    return flags
