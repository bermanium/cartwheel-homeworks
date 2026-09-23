"""Backend for the Homework 4 review interface.

A Python standard-library HTTP server with no dependencies, serving the
single-file app in ``ui/`` and a JSON API over the plain files in
``analysis/state/``. Langfuse stays canonical for accepted binary judgments;
the state files are the inspectable local mirror the handout asks to commit.

Run it:

    .venv/bin/python -m analysis.review_app.server
    .venv/bin/python -m analysis.review_app.server --port 8031

What this keeps from the reference server (``analysis/server.py``)
------------------------------------------------------------------
The file-backed API shape: one state file per endpoint, GET reads, POST
overwrites, atomic temp-and-replace writes, and a permissive CORS header for a
local single-user tool. That contract is what the error-analysis helpers and
``review-loop.md``'s annotation watcher already expect, so keeping it means the
agent-side tooling works unchanged.

What this adds
--------------
``GET /api/traces`` serves conversations rebuilt by ``conversation.py`` rather
than raw span trees, so the browser renders a chronological spine instead of
reassembling one in JavaScript.

``/api/labels`` implements the structured labeling pass the reference server
has no route for. Labels are append-only per the skill's guardrail: flipping a
judgment supersedes the old line instead of overwriting it, so the flip history
survives in the committed artifact.

``POST /api/labels/sync`` writes accepted judgments to Langfuse as scores. It
is a deliberate button press rather than an automatic write on every keystroke,
because the score-config path had never been exercised in this project and
Langfuse Cloud rate-limits writes.
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from analysis.review_app.conversation import build_conversation, outlier_flags

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent
STATE_DIR = REPO_ROOT / "analysis" / "state"
LABELS_DIR = STATE_DIR / "labels"
UI_DIR = HERE / "ui"

API_FILES: dict[str, Path] = {
    "/api/annotations": STATE_DIR / "annotations.json",
    "/api/patterns": STATE_DIR / "patterns.json",
    "/api/suggestions": STATE_DIR / "suggestions.json",
}

API_DEFAULTS: dict[str, Any] = {
    "/api/annotations": [],
    "/api/patterns": {},
    "/api/suggestions": [],
}


# ---------------------------------------------------------------------------
# state files
# ---------------------------------------------------------------------------


def _read_json(path: Path, default: Any) -> Any:
    """Return the parsed JSON at ``path``, or ``default`` if missing or bad."""
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text() or "null") or default
    except (json.JSONDecodeError, OSError):
        return default


def _write_json(path: Path, data: Any) -> None:
    """Write ``data`` atomically (temp file, then replace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.replace(path)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _annotations() -> list[dict[str, Any]]:
    """Read annotations, accepting a bare list or ``{"annotations": [...]}``."""
    data = _read_json(API_FILES["/api/annotations"], [])
    if isinstance(data, dict):
        data = data.get("annotations", [])
    return [a for a in data if isinstance(a, dict)] if isinstance(data, list) else []


# ---------------------------------------------------------------------------
# labels: append-only JSON Lines, one file per mode
# ---------------------------------------------------------------------------


def _label_path(mode: str) -> Path:
    safe = "".join(c for c in mode if c.isalnum() or c in "_-")
    return LABELS_DIR / f"{safe}.jsonl"


def _read_label_lines(mode: str) -> list[dict[str, Any]]:
    path = _label_path(mode)
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _active_labels(mode: str) -> dict[str, dict[str, Any]]:
    """Return the current label per trace, ignoring superseded lines."""
    active: dict[str, dict[str, Any]] = {}
    for line in _read_label_lines(mode):
        if line.get("superseded_by"):
            continue
        tid = line.get("trace_id")
        if tid:
            active[str(tid)] = line
    return active


def taxonomy() -> dict[str, Any]:
    """Return the taxonomy keyed by mode name.

    Two shapes exist on disk. The skill specifies ``{mode_name: {...}}``, which
    is what the agent writes during axial coding, but the committed starter
    file is ``{"modes": [...]}``. Accept both so a fresh checkout renders and
    the file is not rewritten into a shape the helpers do not expect.
    """
    raw = _read_json(API_FILES["/api/patterns"], {})
    if not isinstance(raw, dict):
        return {}
    listed = raw.get("modes")
    if isinstance(listed, list):
        return {
            str(m["name"]): m
            for m in listed
            if isinstance(m, dict) and m.get("name")
        }
    return {k: v for k, v in raw.items() if isinstance(v, dict)}


def _all_modes() -> list[str]:
    """Modes with a label file, plus every mode named in the taxonomy."""
    modes = {p.stem for p in LABELS_DIR.glob("*.jsonl") if not p.stem.startswith("_")}
    return sorted(modes | set(taxonomy()))


def _write_label(mode: str, trace_id: str, label: int, note: str, source: str) -> dict:
    """Append one label line, superseding any previous active line.

    Append-only is the skill's guardrail: a flip must stay inspectable, because
    the iteration log reports label flips alongside judge revisions. Rewriting
    the line in place would erase exactly the history the homework asks to keep.
    """
    LABELS_DIR.mkdir(parents=True, exist_ok=True)
    path = _label_path(mode)
    lines = _read_label_lines(mode)

    new_id = f"l{int(time.time() * 1000)}"
    changed = False
    for line in lines:
        if str(line.get("trace_id")) == str(trace_id) and not line.get("superseded_by"):
            line["superseded_by"] = new_id
            changed = True

    record = {
        "id": new_id,
        "trace_id": str(trace_id),
        "mode": mode,
        "label": int(label),
        "note": note or "",
        "source": source or "human",
        "ts": _now(),
    }
    lines.append(record)
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))
    if changed:
        record["flipped"] = True
    return record


# ---------------------------------------------------------------------------
# traces
# ---------------------------------------------------------------------------


def _percentiles(records: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    stats: dict[str, dict[str, float]] = {}
    for key in ("tool_call_count", "turn_count"):
        values = sorted(
            v
            for r in records
            if isinstance(v := (r.get("features") or {}).get(key), (int, float))
        )
        if not values:
            continue
        stats[key] = {
            "median": statistics.median(values),
            "p90": values[min(len(values) - 1, int(len(values) * 0.90))],
        }
    return stats


_STORE_STATS: dict[str, dict[str, float]] | None = None


def _feature_stats(records: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    """Percentiles for the outlier badges, taken over the **whole store**.

    Computed from the full Module 1 export rather than the loaded batch, so a
    badge means "unusual for Cartwheel" and not "unusual among the 30 traces
    that happen to be open". Measured against a 30-trace batch the same rule
    flagged 14 of them, which is not a signal.

    Cached after the first call: the export is 11.7 MB and the distribution
    does not change while the server runs. Falls back to the loaded records if
    the export is absent.
    """
    global _STORE_STATS
    if _STORE_STATS is not None:
        return _STORE_STATS

    export = REPO_ROOT / "traces" / "support_traces.json"
    if export.exists():
        try:
            from analysis.helpers.normalization import normalize_traces

            payload = json.loads(export.read_text())
            raw = payload["traces"] if isinstance(payload, dict) else payload
            _STORE_STATS = _percentiles(normalize_traces(raw))
            return _STORE_STATS
        except Exception as exc:  # pragma: no cover - fallback path
            print(f"  (store percentiles unavailable, using the batch: {exc})")

    _STORE_STATS = _percentiles(records)
    return _STORE_STATS


def load_traces() -> list[dict[str, Any]]:
    """Build the payload the interface renders, grouped and chronological.

    Reads ``state/samples.json`` (written by ``hw4_prepare_review.py``) and
    ``state/sample_manifest.json``, which records why each trace was selected.
    The manifest reason is carried through so the reviewed sample's composition
    can be reported without re-deriving it.
    """
    records = _read_json(STATE_DIR / "samples.json", [])
    manifest = _read_json(STATE_DIR / "sample_manifest.json", [])
    reasons = {
        m.get("trace_id"): m
        for m in (manifest if isinstance(manifest, list) else [])
        if isinstance(m, dict)
    }
    stats = _feature_stats(records)

    out = []
    for record in records:
        if not isinstance(record, dict):
            continue
        conversation = build_conversation(record)
        meta = record.get("meta") or {}
        features = record.get("features") or {}
        selection = reasons.get(record.get("id"), {})
        out.append(
            {
                "trace_id": record.get("id"),
                "scenario_id": meta.get("scenario_id"),
                "session_id": (record.get("metadata") or {}).get(
                    "cartwheel.session_id"
                ),
                "role": meta.get("role"),
                "store": meta.get("store"),
                "prompt_version": meta.get("prompt_version"),
                "timestamp": record.get("timestamp"),
                "features": features,
                "flags": outlier_flags(record, stats),
                "selection": {
                    "reason": selection.get("reason"),
                    "strategy": selection.get("strategy"),
                },
                "conversation": conversation,
            }
        )
    # Reviewed traces first, then everything still to read. Within the reviewed
    # block, scenario order, so an earlier trace stays findable when a mode
    # discovered late sends the reviewer back to it. Within the unreviewed
    # block, staging order, so batch 1 stragglers come up before batch 2 rather
    # than being scattered through it by scenario id.
    position = {trace_id: i for i, trace_id in enumerate(reasons)}
    annotated = {a.get("trace_id") for a in _annotations() if a.get("trace_id")}
    out.sort(
        key=lambda t: (
            t["trace_id"] not in annotated,
            str(t.get("scenario_id") or "")
            if t["trace_id"] in annotated
            else position.get(t["trace_id"], 10**6),
        )
    )
    return out


# ---------------------------------------------------------------------------
# Langfuse sync
# ---------------------------------------------------------------------------


SYNC_MARKER = STATE_DIR / ".langfuse_sync.json"


def sync_labels_to_langfuse(force: bool = False) -> dict[str, Any]:
    """Write every active label to Langfuse as a score.

    Two deliberate differences from ``langfuse_io.write_label_score``:

    - **One flush for the whole batch.** The helper flushes per score, which
      turns 864 labels into 864 round trips. ``create_score`` queues locally,
      so queueing the batch and flushing once is the same result in a fraction
      of the time.
    - **Refuses to run twice.** ``create_score`` has no idempotency key, so a
      second sync would add a *duplicate* score to every trace rather than
      replacing the first. A marker file records that a sync completed; pass
      ``force`` only after deleting the earlier scores.

    Returns a report rather than raising, so a partial failure still says
    exactly how far it got. The JSON Lines files are the mirror and are never
    modified here.
    """
    try:
        from analysis.helpers import langfuse_io
    except Exception as exc:  # pragma: no cover - import-time only
        return {"ok": False, "error": f"langfuse helpers unavailable: {exc}"}
    if not langfuse_io.is_configured():
        return {"ok": False, "error": "LANGFUSE_* environment is not set"}

    if SYNC_MARKER.exists() and not force:
        previous = _read_json(SYNC_MARKER, {})
        return {
            "ok": False,
            "error": (
                f"already synced {previous.get('written', '?')} scores at "
                f"{previous.get('ts', 'unknown time')}. Re-running would create "
                f"duplicate scores. Delete the existing scores first, then sync "
                f"with force."
            ),
        }

    client = langfuse_io._client()
    # Never push the starter repo's synthetic demo ids; they match no trace
    # in Langfuse and the score would dangle.
    in_set = {t["trace_id"] for t in load_traces()}
    queued, failed, errors = 0, 0, []
    for mode in _all_modes():
        for trace_id, line in _active_labels(mode).items():
            if trace_id not in in_set:
                continue
            try:
                client.create_score(
                    name=mode,
                    value=int(line.get("label", 0)),
                    trace_id=trace_id,
                    data_type="NUMERIC",
                    comment=line.get("note") or None,
                )
                queued += 1
            except Exception as exc:
                failed += 1
                if len(errors) < 5:
                    errors.append(f"{mode}/{trace_id[:8]}: {exc}")
    try:
        client.flush()
    except Exception as exc:
        return {"ok": False, "queued": queued, "error": f"flush failed: {exc}"}

    _write_json(SYNC_MARKER, {"written": queued, "failed": failed, "ts": _now()})
    return {
        "ok": failed == 0,
        "written": queued,
        "failed": failed,
        "errors": errors,
        "note": "Langfuse ingests scores asynchronously; allow a minute before reading back.",
    }


# ---------------------------------------------------------------------------
# progress
# ---------------------------------------------------------------------------


def progress() -> dict[str, Any]:
    """Counts the review summary has to report, derived from committed state."""
    traces = load_traces()
    trace_ids = [t["trace_id"] for t in traces]
    annotations = _annotations()
    annotated = {a.get("trace_id") for a in annotations if a.get("trace_id")}
    no_failure = {
        a.get("trace_id")
        for a in annotations
        if str(a.get("note", "")).strip().lower().startswith("no failure")
    }
    modes = _all_modes()

    # Count only labels on traces actually in the review set. The starter repo
    # ships `labels/unsupported_policy_claim.jsonl` with 120 demo labels on
    # synthetic ids (`upc-fail-tr-00`), and counting those would report a mode
    # the reviewer never labeled and a sample fraction over traces that do not
    # exist. They are reported separately as `foreign` rather than deleted.
    in_set = set(trace_ids)
    per_mode = []
    for mode in modes:
        active = _active_labels(mode)
        mine = {t: line for t, line in active.items() if t in in_set}
        fails = sum(1 for line in mine.values() if int(line.get("label", 0)) == 1)
        per_mode.append(
            {
                "mode": mode,
                "labeled": len(mine),
                "fail": fails,
                "pass": len(mine) - fails,
                "missing": max(0, len(trace_ids) - len(mine)),
                "fraction": round(fails / len(mine), 3) if mine else None,
                "foreign": len(active) - len(mine),
            }
        )

    by_strategy: dict[str, int] = {}
    for t in traces:
        key = str((t.get("selection") or {}).get("strategy") or "unrecorded")
        by_strategy[key] = by_strategy.get(key, 0) + 1

    return {
        "traces_loaded": len(traces),
        "traces_annotated": len(annotated & set(trace_ids)),
        "annotations": len(annotations),
        "no_failure_observed": len(no_failure),
        "modes": per_mode,
        "sample_composition": by_strategy,
        "labeling_complete": all(m["missing"] == 0 for m in per_mode) if modes else False,
    }


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


class ReviewHandler(BaseHTTPRequestHandler):
    """Serves the UI and the file-backed JSON API."""

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A002
        return

    def _send_json(self, data: Any, status: int = 200) -> None:
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> Any:
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            return None
        try:
            return json.loads(self.rfile.read(length))
        except json.JSONDecodeError:
            return None

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._send_json({}, status=204)

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]

        if path in ("/", "/index.html"):
            page = UI_DIR / "index.html"
            if not page.exists():
                self._send_json({"error": "ui/index.html is missing"}, status=404)
                return
            body = page.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/api/traces":
            self._send_json(load_traces())
            return

        if path == "/api/labels":
            self._send_json(
                {mode: list(_active_labels(mode).values()) for mode in _all_modes()}
            )
            return

        if path == "/api/progress":
            self._send_json(progress())
            return

        if path == "/api/patterns":
            self._send_json(taxonomy())
            return

        if path in API_FILES:
            self._send_json(_read_json(API_FILES[path], API_DEFAULTS[path]))
            return

        self._send_json({"error": f"unknown path: {path}"}, status=404)

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        data = self._read_body()

        if path == "/api/labels/sync":
            force = bool(isinstance(data, dict) and data.get("force"))
            self._send_json(sync_labels_to_langfuse(force=force))
            return

        if path == "/api/labels":
            if not isinstance(data, dict) or not data.get("mode"):
                self._send_json({"error": "expected {mode, trace_id, label}"}, 400)
                return
            record = _write_label(
                mode=str(data["mode"]),
                trace_id=str(data.get("trace_id") or ""),
                label=int(data.get("label") or 0),
                note=str(data.get("note") or ""),
                source=str(data.get("source") or "human"),
            )
            self._send_json({"ok": True, "record": record})
            return

        if path in API_FILES:
            if data is None:
                self._send_json({"error": "expected a JSON body"}, status=400)
                return
            _write_json(API_FILES[path], data)
            count = len(data) if isinstance(data, (list, dict)) else 0
            self._send_json({"ok": True, "count": count})
            return

        self._send_json({"error": f"cannot POST to {path}"}, status=404)


def main() -> None:
    parser = argparse.ArgumentParser(description="Homework 4 review interface")
    parser.add_argument("--port", type=int, default=8030)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    LABELS_DIR.mkdir(parents=True, exist_ok=True)

    # The review loop itself is file-backed and needs no credentials, but
    # `/api/labels/sync` writes Langfuse scores, so load `.env` the same way the
    # rest of the repo does. Without this the sync reports "LANGFUSE_* is not
    # set" even though the keys are on disk.
    try:
        from dotenv import load_dotenv

        load_dotenv(REPO_ROOT / ".env")
    except Exception:
        pass
    try:
        from analysis.helpers import langfuse_io

        print(f"  Langfuse: {'configured' if langfuse_io.is_configured() else 'NOT configured'}")
    except Exception:
        pass

    traces = load_traces()
    print(f"review interface on http://{args.host}:{args.port}/")
    print(f"  {len(traces)} traces from {STATE_DIR / 'samples.json'}")
    print(f"  {len(_annotations())} annotations, {len(_all_modes())} modes")
    print("  read a trace top to bottom, select the failing text, type a note")

    server = ThreadingHTTPServer((args.host, args.port), ReviewHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
        server.shutdown()


if __name__ == "__main__":
    main()
