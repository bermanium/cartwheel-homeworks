"""Build profile/results/costs.csv: model cost by call site.

No model calls. Inputs, all saved earlier:

- Customer conversation calls: `traces/support_traces.json` (Homework 3)
  gives the call count. Its token fields are all zero, because the tracing
  did not record usage, so tokens are estimated from the Homework 8 runs in
  `optimize/results/`, whose counts come from the LLM API: average tokens per
  conversation there, times the number of Homework 3 conversations.
- Judge calls: the call counts come from the saved verdicts (Homework 5,
  `analysis/state/judges/`) and run summaries (Homework 7,
  `monitoring/output/`). Neither saved token counts, so tokens come from
  `profile/results/judge_token_sample.json`, where
  `measure_judge_tokens.py` re-sent 10 real judge requests:
    * input tokens are predicted per call in two parts: the fixed text
      (system message, judge prompt, wrapper) at the sample's tokens per
      character of fixed text, plus the conversation at the sample's tokens
      per conversation character (a straight-line fit on the sample). The
      two rates differ because conversations carry dense JSON tool results,
      and the split matters because judge v0 and v1 had shorter prompts;
    * output tokens, mostly reasoning, do not follow length, so each call
      gets the sample's average.

    .venv/bin/python profile/scripts/build_costs.py
"""

from __future__ import annotations

import csv
import glob
import json
import os
import statistics
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

OUT_PATH = REPO_ROOT / "profile" / "results" / "costs.csv"
SAMPLE_PATH = REPO_ROOT / "profile" / "results" / "judge_token_sample.json"
FIELDS = ["cost_category", "call_site", "purpose", "model", "calls",
          "input_tokens", "output_tokens", "cost_usd"]
# Fixed text around every judge prompt; see the MapOp prompt in
# analysis/helpers/scale.py and monitoring/run_judges.py.
JUDGE_WRAPPER_CHARS = len(
    "\n\n--- Trace to evaluate ---\n\n\n"
    "First write a critique of the trace against the criterion. "
    "Use specific evidence from the provided trace. Then return result "
    "as exactly Pass when the named failure is absent, or Fail when present."
)


def read_json(path: Path | str):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def prices(model: str) -> tuple[float, float]:
    table = read_json(REPO_ROOT / "optimize" / "config.json")["prices_per_million_tokens_usd"]
    p = table[model]
    return float(p["input"]), float(p["output"])


def cost(model: str, input_tokens: int, output_tokens: int) -> float:
    input_price, output_price = prices(model)
    return round((input_tokens * input_price + output_tokens * output_price) / 1_000_000, 6)


def customer_row() -> dict:
    traces = read_json(REPO_ROOT / "traces" / "support_traces.json")["traces"]
    generations = [o for t in traces for o in t["observations"] if o.get("type") == "GENERATION"]
    models = {g["model"] for g in generations}
    assert len(models) == 1, models
    model = models.pop()
    assert not any((g.get("usage") or {}).get("input") for g in generations), "traces now carry tokens"

    runs = [read_json(f) for f in glob.glob(str(REPO_ROOT / "optimize" / "results" / "*.json"))]
    runs = [r for r in runs if r.get("model") == model and r.get("evaluated_case_runs")]
    conversations = sum(r["evaluated_case_runs"] for r in runs)
    per_conv_in = sum(r["input_tokens"] for r in runs) / conversations
    per_conv_out = sum(r["output_tokens"] for r in runs) / conversations

    input_tokens = round(per_conv_in * len(traces))
    output_tokens = round(per_conv_out * len(traces))
    return {
        "cost_category": "customer_conversation",
        "call_site": "agent/agent.py:650",
        "purpose": (
            f"Support agent turns (Agents SDK via LiteLLM) for the {len(traces)} Homework 3 "
            f"conversations; calls counted from the traces; tokens ESTIMATED: traces saved no "
            f"token counts, so {per_conv_in:,.0f} in / {per_conv_out:,.0f} out per conversation, "
            f"the API-reported average over {conversations} Homework 8 development runs"
        ),
        "model": model,
        "calls": len(generations),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost_usd": cost(model, input_tokens, output_tokens),
    }


def judge_token_model() -> tuple[float, float, float, int]:
    """(tokens for the fixed v2 text, tokens per conversation character,
    average output tokens, sample size) from the measured sample."""
    samples = [s for s in read_json(SAMPLE_PATH)["samples"].values() if s.get("usage")]
    x = [s["conversation_chars"] for s in samples]
    y = [s["usage"]["input_tokens"] for s in samples]
    mx, my = statistics.mean(x), statistics.mean(y)
    b = sum((xi - mx) * (yi - my) for xi, yi in zip(x, y)) / sum((xi - mx) ** 2 for xi in x)
    a = my - b * mx
    out = statistics.mean(s["usage"]["output_tokens"] for s in samples)
    return a, b, out, len(samples)


def judge_rows() -> list[dict]:
    os.environ["CARTWHEEL_JUDGE_TRACE_SOURCE"] = str(REPO_ROOT / "analysis" / "state" / "hw5_trace_inputs.json")
    from analysis.helpers.scale import load_store_traces

    fixed_v2_tokens, per_conv_char, out_per_call, n = judge_token_model()
    sample = read_json(SAMPLE_PATH)["samples"].values()
    judges_dir = REPO_ROOT / "analysis" / "state" / "judges"
    v2 = read_json(judges_dir / "unsolicited_content-v2.json")
    # Recover the fixed system message DocETL adds from one measured request.
    one = next(iter(sample))
    system_chars = one["request_chars"] - (len(v2["prompt_text"]) + JUDGE_WRAPPER_CHARS + one["conversation_chars"])
    per_fixed_char = fixed_v2_tokens / (system_chars + len(v2["prompt_text"]) + JUDGE_WRAPPER_CHARS)

    def input_tokens_for(prompt_text: str, conversation: str) -> float:
        fixed = system_chars + len(prompt_text) + JUDGE_WRAPPER_CHARS
        return per_fixed_char * fixed + per_conv_char * len(conversation)
    note = (f"tokens ESTIMATED: no token counts were saved, so input is predicted from each request's "
            f"length and output is {out_per_call:,.0f} per call, both from {n} re-sent requests "
            f"measured with the LLM API (profile/results/judge_token_sample.json)")

    # Homework 5: every saved prediction is one call, per judge version.
    texts = {t["trace_id"]: t["text"] for t in load_store_traces()}
    hw5_calls, hw5_in, models = 0, 0.0, set()
    for path in sorted(judges_dir.glob("unsolicited_content-v*.json")):
        judge = read_json(path)
        models.add(judge["model"])
        for trace_ids in judge["predictions"].values():
            for tid in trace_ids:
                hw5_calls += 1
                hw5_in += input_tokens_for(judge["prompt_text"], texts[tid])
    assert len(models) == 1, models
    model = models.pop()

    # Homework 7: judge calls per run from the run summaries. The conversations
    # judged are saved for the two comparison periods only; the daily runs'
    # calls get the average size of those.
    records = []
    for period in ("before", "after"):
        records += read_json(REPO_ROOT / "monitoring" / "output" / f"records-{period}.json")
    per_record = [input_tokens_for(v2["prompt_text"], r["text"]) for r in records]
    summaries = [read_json(p) for p in sorted((REPO_ROOT / "monitoring" / "output").glob("*.json"))]
    summaries = [s for s in summaries if isinstance(s, dict) and "judge_calls" in s]
    hw7_calls = sum(s["judge_calls"] for s in summaries)
    hw7_in = sum(per_record) + (hw7_calls - len(records)) * statistics.mean(per_record)
    assert all(s["model"] == model for s in summaries)

    rows = []
    for site, purpose, calls, input_total in (
        ("analysis/helpers/scale.py:231",
         "Homework 5 unsolicited_content judge development and test runs (v0, v1, v2)", hw5_calls, hw5_in),
        ("monitoring/background_judge.py:127",
         f"Homework 7 monitoring with the frozen unsolicited_content-v2 judge "
         f"({len(summaries)} runs; reruns reused cached verdicts, so restarted runs may add a few uncounted calls)",
         hw7_calls, hw7_in),
    ):
        input_tokens = round(input_total)
        output_tokens = round(out_per_call * calls)
        rows.append({
            "cost_category": "judge",
            "call_site": site,
            "purpose": f"{purpose}; {note}",
            "model": model,
            "calls": calls,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost_usd": cost(model, input_tokens, output_tokens),
        })
    return rows


def main() -> int:
    rows = [customer_row(), *judge_rows()]
    rows.sort(key=lambda r: r["cost_usd"], reverse=True)
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUT_PATH.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    for r in rows:
        print(f"{r['cost_category']:<22} {r['call_site']:<38} calls={r['calls']:>4} "
              f"in={r['input_tokens']:>9,} out={r['output_tokens']:>9,} ${r['cost_usd']:.4f}")
    print(f"wrote {OUT_PATH.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
