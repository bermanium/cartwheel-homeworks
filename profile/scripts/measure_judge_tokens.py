"""Measure the real token cost of one judge call on a small sample.

Homework 5 and Homework 7 saved every judge verdict but no token counts. This
script re-sends the frozen judge's exact request for a few conversations from
each call site and records the token counts the LLM API returns, including
the reasoning tokens Muse Spark spends before it answers. `build_costs.py`
then scales the sample up to the number of calls each homework made.

Sample: 5 Homework 5 conversations (the development and test sets the judge
was run on) and 5 Homework 7 conversations (the two saved monitoring
periods), chosen at the 5th, 25th, 50th, 75th and 95th percentile of
conversation length, because input tokens grow with length.

Requests are built by `monitoring.background_judge.build_requests`, which
captures the request DocETL would send, and are queued in background mode
because the corporate proxy cuts connections that stay silent for ~60 s.
Verdicts are not uploaded anywhere. Every answer is saved as it lands, and
submitted job ids are kept, so a rerun never pays for the same call twice.

    .venv/bin/python profile/scripts/measure_judge_tokens.py

(Run it by path: `python -m profile...` would load Python's built-in
`profile` module instead of this folder.)
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

JUDGE_ID = "unsolicited_content-v2"
PERCENTILES = (0.05, 0.25, 0.50, 0.75, 0.95)
OUT_PATH = REPO_ROOT / "profile" / "results" / "judge_token_sample.json"
POLL_SECONDS = 15


def hw5_conversations() -> dict[str, str]:
    """Trace id -> the text the Homework 5 judge read, for dev and test."""
    os.environ["CARTWHEEL_JUDGE_TRACE_SOURCE"] = str(
        REPO_ROOT / "analysis" / "state" / "hw5_trace_inputs.json"
    )
    from analysis.helpers.scale import load_store_traces

    texts = {t["trace_id"]: t["text"] for t in load_store_traces()}
    split = json.loads((REPO_ROOT / "analysis" / "state" / "splits.json").read_text())
    judged = split["unsolicited_content"]["dev"] + split["unsolicited_content"]["test"]
    return {tid: texts[tid] for tid in judged}


def hw7_conversations() -> dict[str, str]:
    """Record id -> the text the Homework 7 judge read, for both saved periods."""
    out: dict[str, str] = {}
    for period in ("before", "after"):
        path = REPO_ROOT / "monitoring" / "output" / f"records-{period}.json"
        for record in json.loads(path.read_text()):
            out[record["id"]] = record["text"]
    return out


def pick_by_length(texts: dict[str, str]) -> list[str]:
    """The ids at the chosen percentiles of text length, without repeats."""
    ordered = sorted(texts, key=lambda tid: (len(texts[tid]), tid))
    picked: list[str] = []
    for p in PERCENTILES:
        tid = ordered[round(p * (len(ordered) - 1))]
        if tid not in picked:
            picked.append(tid)
    return picked


def usage_dict(usage: Any) -> dict[str, int]:
    input_details = getattr(usage, "input_tokens_details", None)
    output_details = getattr(usage, "output_tokens_details", None)
    return {
        "input_tokens": int(usage.input_tokens or 0),
        "cached_input_tokens": int(getattr(input_details, "cached_tokens", 0) or 0),
        "output_tokens": int(usage.output_tokens or 0),
        "reasoning_tokens": int(getattr(output_details, "reasoning_tokens", 0) or 0),
    }


def main() -> int:
    from dotenv import load_dotenv

    load_dotenv(REPO_ROOT / ".env")
    from monitoring.background_judge import _client, build_requests, responses_body

    state: dict[str, Any] = json.loads(OUT_PATH.read_text()) if OUT_PATH.exists() else {}
    state.setdefault("judge_id", JUDGE_ID)
    samples: dict[str, dict[str, Any]] = state.setdefault("samples", {})

    sources = {"hw5": hw5_conversations(), "hw7": hw7_conversations()}
    for call_site, texts in sources.items():
        for tid in pick_by_length(texts):
            samples.setdefault(
                f"{call_site}:{tid}",
                {"call_site": call_site, "trace_id": tid, "conversation_chars": len(texts[tid])},
            )

    def save() -> None:
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUT_PATH.write_text(json.dumps(state, indent=1))

    client = _client()
    to_submit = [k for k, s in samples.items() if "job_id" not in s and "usage" not in s]
    if to_submit:
        traces = [
            {"id": key, "text": sources[samples[key]["call_site"]][samples[key]["trace_id"]]}
            for key in to_submit
        ]
        requests = build_requests(JUDGE_ID, traces)
        for key in to_submit:
            request = requests[key]
            samples[key]["request_chars"] = sum(len(str(m.get("content", ""))) for m in request["messages"])
            job = client.responses.create(**responses_body(request))
            samples[key]["job_id"] = job.id
            samples[key]["submitted_at"] = time.time()
            save()
        print(f"submitted {len(to_submit)} judge requests", flush=True)

    pending = [k for k, s in samples.items() if "usage" not in s]
    while pending:
        for key in list(pending):
            sample = samples[key]
            try:
                job = client.responses.retrieve(sample["job_id"])
            except Exception as exc:
                print(f"  poll failed for {key} ({type(exc).__name__}), will retry", flush=True)
                continue
            if job.status in ("queued", "in_progress"):
                continue
            pending.remove(key)
            sample["status"] = job.status
            sample["seconds"] = round(time.time() - sample["submitted_at"], 1)
            if job.usage is not None:
                sample["usage"] = usage_dict(job.usage)
            if job.status == "completed":
                try:
                    sample["verdict"] = json.loads(job.output_text).get("result")
                except Exception:
                    sample["verdict"] = None
            else:
                sample["error"] = str(job.error or job.incomplete_details)
                sample.setdefault("usage", None)
            save()
            print(f"  {key}: {job.status} {sample.get('usage')}", flush=True)
        if pending:
            time.sleep(POLL_SECONDS)

    done = [s for s in samples.values() if s.get("usage")]
    print(f"{len(done)} of {len(samples)} calls have token counts; saved to {OUT_PATH.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
