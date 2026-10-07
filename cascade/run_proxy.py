"""Run the cascade's proxy: a less expensive model with the oracle's judge prompt.

The oracle is the frozen Homework 5 judge `unsolicited_content-v2`; its
verdicts on the development and test labels are already saved in
`analysis/state/judges/`, so only the proxy runs here.

The proxy request is the oracle's request with three differences:

1. the model,
2. one added instruction asking for confidence in the verdict from 0 to 1,
3. a `confidence` number in the answer schema.

Everything else is copied from the oracle's DocETL map operation
(`analysis/helpers/scale.py`): the same system message, the same judge
prompt, the same trace text, the same closing instruction, the same token
ceiling. Requests are queued in background mode (as in Homework 7) because
the corporate proxy cuts connections that stay silent for ~60 s. Every answer
is saved as it lands and submitted job ids are kept, so a rerun never pays
for the same call twice.

    .venv/bin/python -m cascade.run_proxy --model MODEL --split development
    .venv/bin/python -m cascade.run_proxy --model MODEL --smoke   # one request
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
ORACLE_ID = "unsolicited_content-v2"
RESULTS_DIR = REPO_ROOT / "cascade" / "results"
POLL_SECONDS = 15
MAX_OUTPUT_TOKENS = 24000

# Copied from the oracle's request (DocETL's map system message and the
# closing instruction in analysis/helpers/scale.py).
SYSTEM_MESSAGE = (
    "You are a a helpful assistant, helping the user make sense of their data. "
    "The dataset description is: a collection of unstructured documents. You will "
    "be performing a map operation (one input:one output). You will perform the "
    "specified task on the provided data, as precisely and exhaustively (i.e., high "
    "recall) as possible. Respond with a JSON object that follows the required schema."
)
CLOSING_INSTRUCTION = (
    "First write a critique of the trace against the criterion. "
    "Use specific evidence from the provided trace. Then return result "
    "as exactly Pass when the named failure is absent, or Fail when present."
)
CONFIDENCE_INSTRUCTION = (
    "Finally, return confidence: how likely your result is to be correct, as a "
    "number from 0 to 1, where 0.5 means a guess and 1 means certain."
)
SCHEMA = {
    "type": "object",
    "properties": {
        "critique": {"type": "string"},
        "result": {"type": "string"},
        "confidence": {"type": "number"},
    },
    "required": ["critique", "result", "confidence"],
    "additionalProperties": False,
}
SPLITS = {"development": "dev", "test": "test"}


def oracle() -> dict[str, Any]:
    return json.loads((REPO_ROOT / "analysis" / "state" / "judges" / f"{ORACLE_ID}.json").read_text())


def trace_texts() -> dict[str, str]:
    """Trace id -> the text the oracle read in Homework 5."""
    os.environ["CARTWHEEL_JUDGE_TRACE_SOURCE"] = str(
        REPO_ROOT / "analysis" / "state" / "hw5_trace_inputs.json"
    )
    from analysis.helpers.scale import load_store_traces

    return {t["trace_id"]: t["text"] for t in load_store_traces()}


def split_ids(split: str) -> list[str]:
    splits = json.loads((REPO_ROOT / "analysis" / "state" / "splits.json").read_text())
    return splits["unsolicited_content"][SPLITS[split]]


def request_body(model: str, prompt_text: str, trace_text: str) -> dict[str, Any]:
    user = (
        f"{prompt_text}\n\n--- Trace to evaluate ---\n{trace_text}\n\n"
        f"{CLOSING_INSTRUCTION} {CONFIDENCE_INSTRUCTION}"
    )
    return {
        "model": model.removeprefix("openai/"),
        "input": [
            {"role": "system", "content": SYSTEM_MESSAGE},
            {"role": "user", "content": user},
        ],
        "text": {"format": {"type": "json_schema", "name": "structured_output",
                            "schema": SCHEMA, "strict": True}},
        "max_output_tokens": MAX_OUTPUT_TOKENS,
        "background": True,
    }


def parse_answer(text: str) -> dict[str, Any]:
    """Verdict (1 = Pass, 0 = Fail, the oracle's convention) and confidence."""
    answer = json.loads(text)
    verdict = str(answer.get("result", "")).strip().lower().rstrip(".")
    confidence = float(answer["confidence"])
    if not 0.0 <= confidence <= 1.0:
        raise ValueError(f"confidence {confidence} outside 0 to 1")
    return {
        # Same rule as the oracle's decoder: anything not clearly Pass is Fail.
        "verdict": 1 if verdict in {"pass", "passed"} else 0,
        "confidence": confidence,
        "critique": answer.get("critique"),
    }


def run(model: str, trace_ids: list[str], out_path: Path) -> dict[str, Any]:
    from dotenv import load_dotenv
    from openai import OpenAI

    load_dotenv(REPO_ROOT / ".env")
    client = OpenAI(timeout=45, max_retries=5)
    state = json.loads(out_path.read_text()) if out_path.exists() else {
        "model": model, "oracle": ORACLE_ID, "results": {}
    }
    results: dict[str, dict[str, Any]] = state["results"]

    def save() -> None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(state, indent=1))

    texts = trace_texts()
    prompt_text = oracle()["prompt_text"]
    for tid in trace_ids:
        if tid in results:
            continue
        job = client.responses.create(**request_body(model, prompt_text, texts[tid]))
        results[tid] = {"job_id": job.id, "submitted_at": time.time()}
        save()
    print(f"{len(trace_ids)} traces, {sum('usage' not in results[t] for t in trace_ids)} waiting", flush=True)

    pending = [t for t in trace_ids if "usage" not in results[t] and "error" not in results[t]]
    while pending:
        for tid in list(pending):
            row = results[tid]
            try:
                job = client.responses.retrieve(row["job_id"])
            except Exception as exc:
                print(f"  poll failed for {tid[:8]} ({type(exc).__name__}), will retry", flush=True)
                continue
            if job.status in ("queued", "in_progress"):
                continue
            pending.remove(tid)
            row["status"] = job.status
            row["seconds"] = round(time.time() - row["submitted_at"], 1)
            if job.usage is not None:
                details = getattr(job.usage, "output_tokens_details", None)
                row["usage"] = {
                    "input_tokens": int(job.usage.input_tokens or 0),
                    "output_tokens": int(job.usage.output_tokens or 0),
                    "reasoning_tokens": int(getattr(details, "reasoning_tokens", 0) or 0),
                }
            if job.status == "completed":
                try:
                    row.update(parse_answer(job.output_text))
                except Exception as exc:
                    row["error"] = f"unparseable answer: {type(exc).__name__}: {exc}"
                    row["raw_output"] = (job.output_text or "")[:2000]
            else:
                row["error"] = str(job.error or job.incomplete_details)
            save()
            shown = {k: row.get(k) for k in ("status", "verdict", "confidence", "error")}
            print(f"  {tid[:8]}: {shown} {row.get('usage')}", flush=True)
        if pending:
            time.sleep(POLL_SECONDS)
    return state


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", required=True)
    parser.add_argument("--split", choices=sorted(SPLITS))
    parser.add_argument("--smoke", action="store_true", help="one development trace of median length")
    args = parser.parse_args(argv)
    if args.smoke == bool(args.split):
        parser.error("give exactly one of --split or --smoke")

    safe_model = args.model.removeprefix("openai/")
    if args.smoke:
        texts = trace_texts()
        ids = sorted(split_ids("development"), key=lambda t: (len(texts[t]), t))
        trace_ids = [ids[len(ids) // 2]]
        out_path = RESULTS_DIR / f"smoke-{safe_model}.json"
    else:
        trace_ids = split_ids(args.split)
        out_path = RESULTS_DIR / f"proxy-{safe_model}-{args.split}.json"
    state = run(args.model, trace_ids, out_path)
    done = [state["results"][t] for t in trace_ids if "verdict" in state["results"][t]]
    print(f"{len(done)} of {len(trace_ids)} verdicts; saved {out_path.relative_to(REPO_ROOT)}")
    if done:
        print(f"median output tokens {statistics.median(r['usage']['output_tokens'] for r in done):,.0f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
