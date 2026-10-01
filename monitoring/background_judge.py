"""Send the frozen judge's exact requests in background mode.

The corporate proxy on the course laptop drops any connection that is silent
for about 60 seconds, and on a slow endpoint day one judge answer takes longer
than that. The Muse Spark endpoint's Responses API accepts ``background=True``:
the request is queued on the server and polled, so no connection stays open
while the model works.

Nothing about the judge changes. Each request is built by the same DocETL map
operation ``judge_sample`` runs, captured just before it would be sent, and
sent with the same messages, answer schema and token ceiling. The answer is
decoded by the same ``_decode_judge_rows``.
"""

from __future__ import annotations

import copy
import json
import time
import uuid
from pathlib import Path
from typing import Any, Iterator

POLL_SECONDS = 15
# A queued request that has not finished in this long is reported as failed
# for this run; its job id is kept, so the next run resumes polling it.
MAX_WAIT_SECONDS = 2 * 60 * 60
CAPTURE_KEY_PREFIX = "background-capture-"


def build_requests(judge_id: str, traces: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Trace id -> the exact completion request DocETL would send for it."""
    import docetl.operations.utils.api as api
    import litellm

    from monitoring.run_judges import judge_sample

    captured: list[dict[str, Any]] = []

    def capture(**kwargs: Any) -> Any:
        captured.append(copy.deepcopy(kwargs))
        placeholder = json.dumps({"critique": "request captured", "result": "Pass"})
        return litellm.ModelResponse(
            choices=[{"message": {"role": "assistant", "content": placeholder}, "finish_reason": "stop"}],
            model=kwargs["model"],
        )

    real_completion, real_key = api.completion, api.cache_key
    api.completion = capture
    # A fresh key per call: DocETL cannot answer from its cache instead of
    # building the request, and the placeholder never lands under a real key.
    api.cache_key = lambda *args, **kwargs: f"{CAPTURE_KEY_PREFIX}{uuid.uuid4().hex}"
    requests: dict[str, dict[str, Any]] = {}
    try:
        for trace in traces:
            captured.clear()
            judge_sample(judge_id, [trace])
            if len(captured) != 1:
                raise RuntimeError(f"expected one judge request for {trace['id']}, saw {len(captured)}")
            requests[trace["id"]] = captured[0]
    finally:
        api.completion, api.cache_key = real_completion, real_key
        _purge_placeholders()
    return requests


def _purge_placeholders() -> None:
    from docetl.operations.utils.cache import cache

    with cache as c:
        for key in list(c.iterkeys()):
            if str(key).startswith(CAPTURE_KEY_PREFIX):
                c.delete(key)


def responses_body(request: dict[str, Any]) -> dict[str, Any]:
    """The same request, in the Responses API's shape, queued in background."""
    unmapped = set(request) - {"model", "messages", "response_format", "max_tokens"}
    if unmapped:
        raise ValueError(f"judge request has settings with no mapping: {sorted(unmapped)}")
    schema = request["response_format"]["json_schema"]
    model = request["model"].removeprefix("openai/")
    return {
        "model": model,
        "input": request["messages"],
        "text": {
            "format": {
                "type": "json_schema",
                "name": schema["name"],
                "schema": schema["schema"],
                "strict": schema.get("strict", False),
            }
        },
        "max_output_tokens": request["max_tokens"],
        "background": True,
    }


def _client() -> Any:
    from openai import OpenAI

    # Every call here is short (submit or poll), so the proxy cutoff never bites.
    return OpenAI(timeout=45, max_retries=5)


def judge_background(
    judge_id: str, traces: list[dict[str, Any]], jobs_path: Path
) -> Iterator[tuple[str, int | Exception]]:
    """Yield (trace id, failure-positive verdict or the error) as answers land.

    Submitted job ids are kept in ``jobs_path`` until answered, so an
    interrupted run resumes polling instead of paying for the call again.
    """
    from monitoring.run_judges import _decode_judge_rows

    client = _client()
    jobs: dict[str, str] = json.loads(jobs_path.read_text()) if jobs_path.exists() else {}

    def save_jobs() -> None:
        jobs_path.parent.mkdir(parents=True, exist_ok=True)
        jobs_path.write_text(json.dumps(jobs, indent=1))

    to_submit = [t for t in traces if t["id"] not in jobs]
    if to_submit:
        requests = build_requests(judge_id, to_submit)
        for trace in to_submit:
            job = client.responses.create(**responses_body(requests[trace["id"]]))
            jobs[trace["id"]] = job.id
            save_jobs()
        print(f"  submitted {len(to_submit)} background judge requests", flush=True)

    pending = [t["id"] for t in traces]
    started = time.time()
    while pending:
        for trace_id in list(pending):
            try:
                job = client.responses.retrieve(jobs[trace_id])
            except Exception as exc:
                print(f"  poll failed for {trace_id} ({type(exc).__name__}), will retry", flush=True)
                continue
            if job.status in ("queued", "in_progress"):
                continue
            pending.remove(trace_id)
            del jobs[trace_id]
            save_jobs()
            if job.status != "completed":
                yield trace_id, RuntimeError(f"background job {job.status}: {job.error or job.incomplete_details}")
                continue
            try:
                answer = json.loads(job.output_text)
                row = {"trace_id": trace_id, "critique": answer.get("critique"), "result": answer.get("result")}
                pass_positive = _decode_judge_rows([row], [trace_id])
            except Exception as exc:
                yield trace_id, exc
                continue
            yield trace_id, 1 - pass_positive[trace_id]
        if pending:
            if time.time() - started > MAX_WAIT_SECONDS:
                for trace_id in pending:
                    yield trace_id, TimeoutError("background job still running; rerun to keep polling")
                return
            time.sleep(POLL_SECONDS)
