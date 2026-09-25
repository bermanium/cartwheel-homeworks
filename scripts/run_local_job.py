"""Run Cartwheel evaluation cases locally, in place of a Harbor Docker job.

Writes a Harbor-shaped job directory that the supplied
``scripts/summarize_harbor_job.py`` and ``scripts/analyze_harbor_job.py`` read
unchanged. Every job it writes is stamped as a local substitution; see
``replay/local_job.py`` for what that does and does not preserve.

    .venv/bin/python scripts/run_local_job.py --baseline --case e-001 \
        --n-attempts 5 --job-name hw6-baseline-e-001
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

try:
    from dotenv import load_dotenv

    load_dotenv(REPO_ROOT / ".env")
except Exception:  # pragma: no cover - dotenv is optional
    pass

from harbor_adapter.export import load_export_cases  # noqa: E402
from replay.harness import ReplayInfraError  # noqa: E402
from replay.local_job import run_local_job  # noqa: E402
from replay.rollout import load_cases  # noqa: E402

DEFAULT_JOBS_DIR = REPO_ROOT / ".harbor" / "jobs"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=None)
    parser.add_argument(
        "--baseline",
        action="store_true",
        help="run cases that have not been classified yet",
    )
    parser.add_argument(
        "--case",
        action="append",
        dest="case_ids",
        help="run one case id; repeat this option to select more cases",
    )
    parser.add_argument("--n-attempts", type=int, default=5)
    parser.add_argument("--job-name", required=True)
    parser.add_argument("--jobs-dir", type=Path, default=DEFAULT_JOBS_DIR)
    parser.add_argument(
        "--model",
        default=os.environ.get("CARTWHEEL_MODEL"),
        help="defaults to CARTWHEEL_MODEL from the environment or .env",
    )
    args = parser.parse_args()

    if not args.model:
        parser.error("pass --model or set CARTWHEEL_MODEL in .env")

    cases = load_export_cases(args.cases) if args.baseline else load_cases(args.cases)
    if args.case_ids:
        wanted = set(args.case_ids)
        cases = [case for case in cases if case.get("id") in wanted]
        missing = wanted - {str(case.get("id")) for case in cases}
        if missing:
            parser.error(f"unknown case id(s): {', '.join(sorted(missing))}")
    if not cases:
        parser.error("no cases selected")

    job_dir = args.jobs_dir / args.job_name
    total = len(cases) * args.n_attempts
    print(
        f"Running {len(cases)} case(s) x {args.n_attempts} attempts "
        f"= {total} agent runs on {args.model}",
        flush=True,
    )

    def report(case_id: str, index: int, record: dict) -> None:
        verdict = "pass" if record["passed"] else "fail"
        detail = "" if record["passed"] else f"  ({'; '.join(record['failure_modes'])})"
        print(f"  {case_id} attempt {index}: {verdict}{detail}", flush=True)

    try:
        run_local_job(
            job_dir,
            cases,
            model=args.model,
            n_attempts=args.n_attempts,
            on_trial=report,
        )
    except ReplayInfraError as exc:
        print(f"\nInfrastructure failure, job not written: {exc}", file=sys.stderr)
        print(
            "Fix the cause and rerun this case only. Do not classify a case "
            "from a partial baseline.",
            file=sys.stderr,
        )
        return 2

    print(f"\nWrote {job_dir}/result.json")
    print(
        "Summarize it with:\n"
        f"  .venv/bin/python scripts/summarize_harbor_job.py {job_dir} "
        f"--expected-attempts {args.n_attempts}"
        + (" --classify" if args.baseline else "")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
