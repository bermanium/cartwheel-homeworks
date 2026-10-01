"""Bias-corrected failure prevalence for a monitoring period."""

from __future__ import annotations

from typing import Any, Sequence

import numpy as np


def corrected_mode_prevalence(
    sample_preds: Sequence[int],
    test_labels: Sequence[int],
    test_preds: Sequence[int],
    confidence: float = 0.95,
    bootstrap_iterations: int = 20000,
    seed: int | None = 7,
) -> dict[str, Any]:
    """Bias-corrected live prevalence for one mode from sampled verdicts.

    The contract, precisely:

      1. ``raw`` is the uncorrected flag rate: ``mean(sample_preds)``.
      2. Compute the frozen judge's failure sensitivity and pass specificity
         from ``test_labels`` and ``test_preds``. Both use the monitoring
         convention that 1 means a failure is present. Failure sensitivity is
         the flagged fraction of human-labeled failures. Pass specificity is
         the unflagged fraction of human-labeled passes.
      3. Compute the Rogan-Gladen point estimate, then resample the held-out
         records and sampled predictions to obtain a percentile-bootstrap
         interval. Use a seeded NumPy generator so the committed result is
         reproducible.
      4. Resample the monitoring predictions and the paired held-out records
         independently with replacement. Keep their original sample sizes.
         Discard a draw if the correction cannot be computed. Clamp each
         retained estimate to [0, 1], then take the percentile interval.
         Raise ``ValueError`` if no replicate is valid.

    Args:
        sample_preds: the judge's 0/1 verdicts over the UNIFORM BASE sample
            only (never the risk strata; they are biased toward failure by
            design).
        test_labels: human labels for the frozen Homework 5 judge's test
            split.
        test_preds: the frozen judge's predictions on that test split.
        confidence: interval confidence level.
        bootstrap_iterations: number of percentile-bootstrap replicates.
        seed: numpy seed for a reproducible interval; None leaves the RNG
            untouched.

    Returns:
        {"raw", "corrected", "ci_low", "ci_high", "confidence",
         "failure_sensitivity", "pass_specificity", "n_sample"}
        with "corrected" clamped to [0, 1] and rates rounded to 4 places.

    Raises:
        ValueError: if an input is empty, the held-out inputs have different
            lengths, a value is not 0 or 1, a class is absent, the judge is
            missing a usable correction, or no bootstrap replicate is valid.
    """
    sample = np.asarray(sample_preds, dtype=int)
    labels = np.asarray(test_labels, dtype=int)
    preds = np.asarray(test_preds, dtype=int)
    if not sample.size or not labels.size:
        raise ValueError("the sample and the held-out records must be nonempty")
    if labels.size != preds.size:
        raise ValueError("held-out labels and predictions differ in length")
    for values in (sample, labels, preds):
        if not np.isin(values, (0, 1)).all():
            raise ValueError("every value must be 0 or 1")
    if labels.all() or not labels.any():
        raise ValueError("the held-out labels need both failures and passes")

    def rates(y: np.ndarray, p: np.ndarray) -> tuple[float, float]:
        """Failure sensitivity and pass specificity, 1 meaning failure."""
        return float(p[y == 1].mean()), float(1 - p[y == 0].mean())

    def rogan_gladen(raw: float, sensitivity: float, specificity: float) -> float:
        return (raw + specificity - 1) / (sensitivity + specificity - 1)

    raw = float(sample.mean())
    sensitivity, specificity = rates(labels, preds)
    if sensitivity + specificity - 1 <= 0:
        raise ValueError(
            "the judge is no better than chance on the held-out set, so the "
            "correction is undefined"
        )
    corrected = min(max(rogan_gladen(raw, sensitivity, specificity), 0.0), 1.0)

    rng = np.random.default_rng(seed) if seed is not None else np.random.default_rng()
    replicates = []
    for _ in range(bootstrap_iterations):
        s = sample[rng.integers(0, sample.size, sample.size)]
        idx = rng.integers(0, labels.size, labels.size)
        y, p = labels[idx], preds[idx]
        if y.all() or not y.any():
            continue
        sens, spec = rates(y, p)
        if sens + spec - 1 <= 0:
            continue
        replicates.append(min(max(rogan_gladen(float(s.mean()), sens, spec), 0.0), 1.0))
    if not replicates:
        raise ValueError("no bootstrap replicate produced a usable correction")
    alpha = (1 - confidence) / 2
    low, high = np.quantile(replicates, [alpha, 1 - alpha])

    return {
        "raw": round(raw, 4),
        "corrected": round(corrected, 4),
        "ci_low": round(float(low), 4),
        "ci_high": round(float(high), 4),
        "confidence": confidence,
        "failure_sensitivity": round(sensitivity, 4),
        "pass_specificity": round(specificity, 4),
        "n_sample": int(sample.size),
    }
