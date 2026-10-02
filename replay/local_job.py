"""Run the Homework 6 evaluation cases without a container runtime.

SUBSTITUTION NOTICE. Harbor runs every trial in a fresh Docker container.
No container runtime can run on this machine: Apple's `container` installs
and passes Gatekeeper, but its API service cannot register with launchd, and
Docker Desktop needs the same background helper. Sending the agent's
credential to a third-party CI provider is not permitted either, so GitHub's
runners are not an alternative. This module therefore plays the same cases
through the same course-supplied engine, in process:

  - ``replay.rollout.world_reset`` re-seeds a deterministic world before every
    trial, so each trial starts from an identical database and a write in one
    trial cannot leak into the next. That is the property the containers were
    there to guarantee, and it survives the substitution.
  - ``replay.harness.replay_case`` fans the trials out under the course's
    retry contract: infrastructure failures are retried, a verdict never is.
  - ``replay.rollout.apply_checks`` runs the case's code checks unchanged.
  - :func:`run_frozen_judge` runs an accepted Homework 5 judge through the
    Homework 5 contract: the same frozen prompt and model, the same DocETL
    map operation over the whole conversation and tool trace, and the same
    strict Pass or Fail parser.

What the substitution loses is isolation of everything *outside* the world:
installed packages, stray files, the host environment. Say so in the write-up
rather than presenting these results as Harbor output.

The job directory this writes has the same shape Harbor's does, so
``harbor_adapter.summary`` and ``harbor_adapter.analysis`` read it unchanged.
Every ``result.json`` carries ``produced_by`` and ``substitution`` fields
naming its real origin.
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from replay.harness import ReplayInfraError, replay_case
from replay.rollout import (
    apply_checks,
    judge_trace_text,
    load_frozen_judge,
    retrieved_docs_text,
    run_case,
    world_reset,
)

PRODUCED_BY = "replay.local_job (local substitution, not a Harbor job)"

# The Homework 5 prompt wrapper, character for character. The frozen judge was
# measured against this text; changing it would measure a different judge.
JUDGE_PROMPT_SUFFIX = (
    "\n\n--- Trace to evaluate ---\n"
    "{{ input.content }}\n\n"
    "First write a critique of the trace against the criterion. "
    "Use specific evidence from the provided trace. Then return result "
    "as exactly Pass when the named failure is absent, or Fail when present."
)


def run_frozen_judge(judge: dict[str, Any], content: str) -> str:
    """Score one trace with a frozen judge. Returns "pass" or "fail"."""
    return run_frozen_judge_with_critique(judge, content)[0]


def run_frozen_judge_with_critique(
    judge: dict[str, Any], content: str
) -> tuple[str, str]:
    """Score one trace with a frozen judge, under the Homework 5 contract.

    Two settings are not DocETL defaults and are not optional here. The
    endpoint serving the course model rejects a forced function call, so the
    schema is requested as structured output instead; and the model spends
    output budget on reasoning before answering, so a low ceiling returns no
    content at all. Homework 5 established both. Dropping either does not
    degrade the judge, it stops it returning anything.

    Returns "pass" or "fail" and the judge's critique. Raises on a malformed
    result: an unparseable verdict is an infrastructure failure, never a
    silent Fail.
    """
    from docetl.api import Dataset, MapOp, Pipeline, PipelineOutput, PipelineStep

    with tempfile.TemporaryDirectory(prefix="cartwheel-judge-") as directory:
        root = Path(directory)
        input_path = root / "input.json"
        output_path = root / "output.json"
        input_path.write_text(
            json.dumps([{"trace_id": "current", "content": content}]),
            encoding="utf-8",
        )
        operation = MapOp(
            name="classify_failure_mode",
            type="map",
            model=judge["model"],
            prompt=judge["prompt_text"] + JUDGE_PROMPT_SUFFIX,
            output={
                "schema": {"critique": "string", "result": "string"},
                "mode": "structured_output",
            },
            litellm_completion_kwargs={"max_tokens": 24000},
        )
        pipeline = Pipeline(
            name="cartwheel_judge_local",
            datasets={"traces": Dataset(type="file", path=str(input_path))},
            operations=[operation],
            steps=[
                PipelineStep(
                    name="classify",
                    input="traces",
                    operations=["classify_failure_mode"],
                )
            ],
            output=PipelineOutput(
                type="file",
                path=str(output_path),
                intermediate_dir=str(root),
            ),
        )
        pipeline.run()
        rows = json.loads(output_path.read_text(encoding="utf-8"))

    if len(rows) != 1:
        raise ValueError(f"the judge returned {len(rows)} results, expected 1")
    verdict = rows[0].get("result")
    critique = rows[0].get("critique")
    if verdict not in ("Pass", "Fail"):
        raise ValueError(f"judge result must be Pass or Fail, got {verdict!r}")
    if not isinstance(critique, str) or not critique.strip():
        raise ValueError("judge result needs a critique")
    return ("pass" if verdict == "Pass" else "fail"), critique


def _trial_runner(
    case: dict[str, Any],
    judges: dict[str, dict[str, Any]],
    model: str,
    state_root: Path,
) -> Callable[[], dict[str, Any]]:
    """One rollout: run the agent, apply the checks, run any judge."""
    expected_judges = case["expected"].get("judges", {})

    def runner() -> dict[str, Any]:
        try:
            transcript = run_case(case, model=model)
        except Exception as exc:  # a transport or endpoint failure, not a verdict
            raise ReplayInfraError(f"agent run failed: {exc}") from exc

        outcome = apply_checks(case, transcript, state_root / "cartwheel.db")
        failure_modes = list(outcome["failed"])
        rewards: dict[str, float] = {"checks": 1.0 if outcome["passed"] else 0.0}
        verdicts: dict[str, str] = {}

        for mode, expected in expected_judges.items():
            try:
                verdict = run_frozen_judge(judges[mode], judge_trace_text(transcript))
            except Exception as exc:  # an unscored trace is infrastructure
                raise ReplayInfraError(f"judge {mode!r} failed: {exc}") from exc
            verdicts[mode] = verdict
            agrees = verdict == expected
            rewards[f"judge_{mode}"] = 1.0 if agrees else 0.0
            if not agrees:
                failure_modes.append(f"judge {mode}: {verdict}, expected {expected}")

        passed = all(value >= 1.0 for value in rewards.values())
        rewards["reward"] = 1.0 if passed else 0.0
        return {
            "passed": passed,
            "steps": transcript["steps"],
            "rewards": rewards,
            "failure_modes": failure_modes,
            "judge_verdicts": verdicts,
            "transcript": transcript,
            "retrieved_policy_documents": retrieved_docs_text(transcript),
        }

    return runner


def run_local_job(
    job_dir: Path,
    cases: list[dict[str, Any]],
    *,
    model: str,
    n_attempts: int,
    max_infra_retries: int = 2,
    on_trial: Callable[[str, int, dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Run every case ``n_attempts`` times and write a Harbor-shaped job.

    Raises ReplayInfraError if one case still fails after its retries, which
    leaves that case unclassifiable. Fix the cause and rerun that case only,
    as the handout instructs; do not accept a partial baseline.
    """
    if n_attempts < 1:
        raise ValueError("n_attempts must be at least 1")
    if not cases:
        raise ValueError("no cases to run")

    provider, _, bare_model = model.partition("/")
    if not bare_model:
        provider, bare_model = "", model

    job_dir = Path(job_dir)
    job_dir.mkdir(parents=True, exist_ok=True)
    state_root = job_dir / "world"
    trial_results: list[dict[str, Any]] = []

    for case in cases:
        case_id = case["id"]
        judges = {
            mode: load_frozen_judge(mode)
            for mode in case["expected"].get("judges", {})
        }
        records = replay_case(
            _trial_runner(case, judges, model, state_root),
            world_reset(state_root),
            n=n_attempts,
            max_infra_retries=max_infra_retries,
        )
        for record in records:
            index = record["rollout"] + 1
            trial_name = f"{case_id}.{index}"
            evidence_dir = job_dir / "trials" / trial_name
            evidence_dir.mkdir(parents=True, exist_ok=True)
            (evidence_dir / "cartwheel-result.json").write_text(
                json.dumps(
                    {
                        "case_id": case_id,
                        "kind": case.get("kind", "unclassified"),
                        "transcript": record["transcript"],
                        "retrieved_policy_documents": record[
                            "retrieved_policy_documents"
                        ],
                        "failure_modes": record["failure_modes"],
                        "judge_verdicts": record["judge_verdicts"],
                    },
                    indent=2,
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            trial_results.append(
                {
                    "task_name": f"cartwheel/evals__{case_id}",
                    "trial_name": trial_name,
                    "verifier_result": {"rewards": record["rewards"]},
                    "exception_info": None,
                    "agent_info": {
                        "name": "cartwheel",
                        "model_info": {"name": bare_model, "provider": provider},
                    },
                    "failure_modes": record["failure_modes"],
                    "steps": record["steps"],
                }
            )
            if on_trial is not None:
                on_trial(case_id, index, record)

    result = {
        "produced_by": PRODUCED_BY,
        "substitution": (
            "Trials ran in process against a freshly seeded world, not in a "
            "Harbor Docker container. See the module docstring for why."
        ),
        "job_name": job_dir.name,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "model": model,
        "n_attempts": n_attempts,
        "trial_results": trial_results,
    }
    (job_dir / "result.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return result
