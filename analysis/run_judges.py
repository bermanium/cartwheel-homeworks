"""Homework 5: build the judge's inputs, split the labels, run and score judges.

Run from the repository root:

    python analysis/run_judges.py export     # HW5-convention labels (Pass=1)
    python analysis/run_judges.py prepare    # the judge's reading material
    python analysis/run_judges.py split      # train/dev/test, once, seed 7
    python analysis/run_judges.py develop    # score a prompt version on dev
    python analysis/run_judges.py test       # the frozen judge's test metrics

``develop`` calls the model. ``test`` does not: it rescores predictions already
cached on the judge record, so it reproduces the reported numbers in under a
second and can be re-run safely. The one-time freeze and test run that produced
those predictions lives in ``hw5_run_test.py``.

``develop`` and ``test`` read the traces through
``analysis.helpers.scale.load_store_traces``, which falls back to Langfuse
unless ``CARTWHEEL_JUDGE_TRACE_SOURCE`` names the local export. Langfuse Cloud
caps per-trace reads at 15/min and will fail partway through a batch, so set::

    export CARTWHEEL_JUDGE_TRACE_SOURCE=analysis/state/hw5_trace_inputs.json

Two conventions collide in this assignment and the collision is silent, so it
is worth stating once. Homework 4 stored ``1`` for *the failure is present*.
Homework 5 wants ``1`` for *Pass*, meaning the failure is absent.
:func:`export_hw5_labels` writes the flipped copy to
``state/hw5_labels/<mode>.jsonl``; ``analysis.helpers.tools._load_labels``
prefers that file when it exists and flips it back for its own internal use.
The Homework 4 files are never rewritten.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from analysis.helpers import split_labels  # noqa: E402
from analysis.helpers._state import state_path  # noqa: E402
from analysis.review_app.conversation import build_conversation  # noqa: E402

# The judge model and its API key live in `.env`, the same place the agent and
# the review server read them from. Without this the model name is missing and
# the run fails after the prompt is already registered.
try:
    from dotenv import load_dotenv

    load_dotenv(REPO_ROOT / ".env")
except Exception:  # pragma: no cover - dotenv is optional
    pass

MODE = "unsolicited_content"
INPUTS_PATH = state_path("hw5_trace_inputs.json")

# Keys on a sample record that would hand the judge the answer or a hint at it:
# the reviewer's own labels and notes, the scenario the trace was generated
# from, and the selection reason that says why it was pulled for review.
BANNED_RECORD_KEYS = {
    "label", "labels", "note", "notes", "annotation", "annotations",
    "scenario_id", "cartwheel_scenario_id", "selection", "reason", "mode",
    "features", "flags", "suggestions",
}


# ---------------------------------------------------------------------------
# Part A tail: the HW5-convention label export
# ---------------------------------------------------------------------------


def export_hw5_labels(mode: str = MODE) -> dict[str, int]:
    """Write ``state/hw5_labels/<mode>.jsonl`` with Pass=1, Fail=0.

    Reads the live label per trace from the Homework 4 file, collapsing the
    append-only history the same way the review app does, and flips the
    convention. The evidence fields the handout asks to keep (the note, the
    provenance, the timestamp) travel with the label.
    """
    source = state_path("labels", f"{mode}.jsonl")
    live: dict[str, dict[str, Any]] = {}
    for line in source.read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("superseded_by"):
            continue
        live[row["trace_id"]] = row

    out_path = state_path("hw5_labels", f"{mode}.jsonl")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    counts = {"pass": 0, "fail": 0}
    for trace_id, row in sorted(live.items()):
        failure_present = int(row["label"])
        record = {
            "trace_id": trace_id,
            "label": 1 - failure_present,  # HW5: 1 = Pass = failure absent
            "note": row.get("note", ""),
            "source": row.get("source", ""),
            "ts": row.get("ts", ""),
            "hw4_label": failure_present,
        }
        counts["fail" if failure_present else "pass"] += 1
        lines.append(json.dumps(record))
    out_path.write_text("\n".join(lines) + "\n")
    return {**counts, "total": len(lines), "path": str(out_path)}


# ---------------------------------------------------------------------------
# Part B: the judge's reading material
# ---------------------------------------------------------------------------


def _messages_for(record: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten one reviewed trace into the messages the judge reads.

    Shapes are chosen for what ``normalization._flatten`` does downstream, not
    for what reads nicely here: it renders a ``tool_call`` from its
    ``arguments`` field and every other role from ``text``, and drops a message
    that has neither. A tool result carrying only a ``result`` key would
    therefore vanish silently, taking the order total with it, and the $100
    ESC-5 boundary cannot be judged without that number.

    The agent's system prompt is deliberately excluded. It is identical across
    all 108 traces, so it distinguishes none of them, and it would roughly
    triple the size of every judge call.
    """
    conversation = build_conversation(record)
    messages: list[dict[str, Any]] = []
    for turn in conversation.get("turns", []):
        if turn.get("user"):
            messages.append({"role": "user", "text": str(turn["user"])})
        for step in turn.get("steps", []):
            if step.get("reasoning"):
                messages.append({"role": "assistant", "text": str(step["reasoning"])})
            name = str(step.get("tool") or "")
            messages.append({
                "role": "tool_call",
                "name": name,
                "arguments": {"tool": name, "args": step.get("arguments")},
            })
            messages.append({
                "role": "tool_result",
                "name": name,
                "text": json.dumps(
                    {"tool": name, "result": step.get("result")},
                    ensure_ascii=False, sort_keys=True, default=str,
                ),
            })
        if turn.get("reply"):
            messages.append({"role": "assistant", "text": str(turn["reply"])})
    return messages


def _assert_no_leakage(records: list[dict[str, Any]]) -> None:
    """Fail loudly if anything that reveals the answer reached the export."""
    for record in records:
        extra = set(record) - {"trace_id", "trace"}
        if extra:
            raise ValueError(f"unexpected keys in a judge input record: {sorted(extra)}")
        for message in record["trace"]:
            banned = set(message) & BANNED_RECORD_KEYS
            if banned:
                raise ValueError(f"leaked keys on a message: {sorted(banned)}")
    blob = json.dumps(records)
    if "support-0" in blob:
        raise ValueError("a scenario id reached the judge inputs")
    if "reviewer_default" in blob:
        raise ValueError("label provenance reached the judge inputs")


def prepare_inputs(mode: str = MODE) -> dict[str, Any]:
    """Write ``state/hw5_trace_inputs.json``: one clean record per labelled trace.

    One record per conversation, ``{"trace_id": ..., "trace": [messages]}``.
    The 108 reviewed traces are 108 distinct scenarios, so no de-duplication of
    repeated runs is needed; the function asserts that rather than assuming it.
    """
    samples = json.loads(state_path("samples.json").read_text())
    labels = {
        json.loads(line)["trace_id"]
        for line in state_path("hw5_labels", f"{mode}.jsonl").read_text().splitlines()
        if line.strip()
    }

    by_id = {record["id"]: record for record in samples if isinstance(record, dict)}
    scenarios = [
        (record.get("meta") or {}).get("scenario_id")
        for tid, record in by_id.items() if tid in labels
    ]
    if len(set(scenarios)) != len(scenarios):
        raise ValueError("two labelled traces share a scenario; de-duplicate first")

    records, empty = [], []
    for trace_id in sorted(labels):
        source = by_id.get(trace_id)
        if source is None:
            raise ValueError(f"labelled trace {trace_id} is not in samples.json")
        messages = _messages_for(source)
        if not messages:
            empty.append(trace_id)
            continue
        records.append({"trace_id": trace_id, "trace": messages})

    if empty:
        raise ValueError(f"{len(empty)} labelled traces rendered no messages: {empty[:3]}")
    _assert_no_leakage(records)

    INPUTS_PATH.write_text(json.dumps(records, ensure_ascii=False, indent=1))
    sizes = sorted(len(json.dumps(r)) for r in records)
    return {
        "records": len(records),
        "labels": len(labels),
        "path": str(INPUTS_PATH),
        "chars_min": sizes[0],
        "chars_median": sizes[len(sizes) // 2],
        "chars_max": sizes[-1],
        "chars_total": sum(sizes),
    }


def split_data(mode: str = MODE) -> dict[str, Any]:
    """Split the labels 20/40/40 with ``validate-evaluator``'s helper, once.

    Seeded and written to ``state/splits.json``. Re-running it after seeing
    results is how a held-out set quietly stops being held out, so this reports
    the existing split instead of redrawing one.
    """
    records = json.loads(INPUTS_PATH.read_text())
    existing = json.loads(state_path("splits.json").read_text()).get(mode)
    if existing:
        return {"already_split": True, **_split_counts(mode, existing)}

    splits = split_labels(
        mode,
        fractions=(0.20, 0.40, 0.40),
        seed=7,
        min_per_class=10,
        eligible_trace_ids=[record["trace_id"] for record in records],
    )
    return {"already_split": False, **_split_counts(mode, splits)}


def _split_counts(mode: str, splits: dict[str, Any]) -> dict[str, Any]:
    """Pass/Fail counts per split, in HW5 terms, read back from the export."""
    labels = {}
    for line in state_path("hw5_labels", f"{mode}.jsonl").read_text().splitlines():
        if line.strip():
            row = json.loads(line)
            labels[row["trace_id"]] = int(row["label"])  # 1 = Pass
    out: dict[str, Any] = {}
    for name in ("train", "dev", "test"):
        ids = splits.get(name, [])
        out[name] = {
            "pass": sum(1 for tid in ids if labels.get(tid) == 1),
            "fail": sum(1 for tid in ids if labels.get(tid) == 0),
            "n": len(ids),
        }
    return out


# ---------------------------------------------------------------------------
# Part C: run a judge version against the development split
# ---------------------------------------------------------------------------


def _existing_judge(mode: str, prompt_text: str, model: str) -> dict[str, str] | None:
    """Return the registered judge for this exact (prompt, model), if any."""
    from analysis.helpers.tools import _prompt_hash

    wanted = _prompt_hash(prompt_text, model)
    history = state_path("judges", f"_history_{mode}.json")
    if not history.exists():
        return None
    for entry in json.loads(history.read_text()).get("versions", []):
        if entry.get("prompt_hash") == wanted:
            return {"judge_id": entry["judge_id"], "prompt_hash": wanted}
    return None


def run_development(
    mode: str = MODE,
    prompt_path: str | Path = "analysis/prompts/unsolicited_content-v0.txt",
    judge_model: str | None = None,
    batch_size: int = 10,
) -> dict[str, Any]:
    """Register a prompt version, run it on dev, and save the metrics.

    The model defaults to ``CARTWHEEL_MODEL``, the same model that drives the
    Cartwheel agent, which is Daniel's decision and a departure from the
    handout's ``gpt-4o-mini``. Using the model under evaluation as its own
    judge risks self-preference, and that risk is measurable here rather than
    hypothetical: it would show up as a low TNR against the human labels.

    Predictions are cached per (prompt hash, trace id). If this is interrupted,
    call it again with the same prompt: completed batches are not re-paid for.
    """
    from analysis.helpers import judge_alignment, register_judge, run_judge

    prompt_text = Path(prompt_path).read_text()
    model = judge_model or os.environ.get("CARTWHEEL_MODEL")
    if not model:
        raise ValueError("no judge model: pass judge_model or set CARTWHEEL_MODEL")

    # register_judge mints a new version on every call. Calling it again for a
    # prompt already on record would orphan that version's cached predictions
    # and re-pay for them, so an unchanged (prompt, model) reuses its judge.
    record = _existing_judge(mode, prompt_text, model)
    if record is None:
        record = register_judge(mode=mode, prompt_text=prompt_text, judge_model=model)
    judge_id = record["judge_id"]
    run_judge(judge_id, split="dev", batch_size=batch_size)
    metrics = judge_alignment(judge_id, split="dev")

    report = {
        "judge_id": judge_id,
        "mode": mode,
        "split": "dev",
        "model": model,
        "prompt_path": str(prompt_path),
        "prompt_hash": record["prompt_hash"],
        **metrics,
    }
    out = REPO_ROOT / "analysis" / "report" / f"dev-{judge_id}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2))
    return report


# ---------------------------------------------------------------------------
# Part D: the frozen judge's test metrics
# ---------------------------------------------------------------------------


def _frozen_judge(mode: str) -> dict[str, Any]:
    """Return the one frozen judge version for ``mode``.

    Raises if there is not exactly one. Two frozen versions would mean the test
    split had been measured more than once, which is the thing the freeze guard
    exists to prevent, so it is worth failing loudly rather than picking one.
    """
    history = state_path("judges", f"_history_{mode}.json")
    if not history.exists():
        raise ValueError(f"no judge history for mode '{mode}'")

    frozen = []
    for entry in json.loads(history.read_text()).get("versions", []):
        path = state_path("judges", f"{entry['judge_id']}.json")
        if not path.exists():
            continue
        record = json.loads(path.read_text())
        if record.get("status") == "frozen":
            frozen.append(record)

    if not frozen:
        raise ValueError(
            f"no frozen judge for mode '{mode}'; the test split stays locked "
            "until freeze_judge is called."
        )
    if len(frozen) > 1:
        ids = ", ".join(sorted(record["judge_id"] for record in frozen))
        raise ValueError(f"more than one frozen judge for '{mode}': {ids}")
    return frozen[0]


def report_test(mode: str = MODE) -> dict[str, Any]:
    """Recompute the frozen judge's test metrics from its cached predictions.

    **No model calls and no new predictions.** ``judge_alignment`` scores the
    predictions already stored on the judge record against the human labels, so
    this reproduces the reported numbers exactly and is safe to re-run — which
    is what the handout asks for on camera.

    It does not repeat the freeze or the test run that produced those
    predictions. The test split is measured once; ``hw5_run_test.py`` is the
    record of that one-time run.
    """
    from analysis.helpers import judge_alignment

    record = _frozen_judge(mode)
    judge_id = record["judge_id"]
    metrics = judge_alignment(judge_id, split="test")

    report = {
        "judge_id": judge_id,
        "mode": mode,
        "split": "test",
        "model": record["model"],
        "prompt_path": f"analysis/prompts/{judge_id}.txt",
        "prompt_hash": record["prompt_hash"],
        "frozen_at": record.get("frozen_at"),
        **metrics,
    }
    out = REPO_ROOT / "analysis" / "report" / f"test-{judge_id}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2))
    return report


COMMANDS = {
    "export": export_hw5_labels,
    "prepare": prepare_inputs,
    "split": split_data,
    "develop": run_development,
    "test": report_test,
}


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[1] not in COMMANDS:
        print(f"usage: python analysis/run_judges.py [{'|'.join(COMMANDS)}]")
        return 2
    print(json.dumps(COMMANDS[argv[1]](), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
