# Monitoring `unsolicited_content` after deployment

Two comparable periods of the same 50 scenarios on Muse Spark 1.3, judged by
the frozen Homework 5 judge `unsolicited_content-v2`. Threshold: 0.30.

| | Before (HW3 run, 2026-09-15) | After (2026-09-29) |
|---|---|---|
| Random sample flagged | 4 of 10 | 4 of 10 |
| Corrected estimate | 0.48 | 0.48 |
| 95% interval | 0.00 to 1.00 | 0.00 to 1.00 |
| Risk groups flagged | 13 of 24 | 16 of 26 |

The corrected estimate adjusts the raw rate for the judge's measured errors
(it catches 65% of real failures and flags 17% of good replies): a true rate
of 0.48 is what makes this judge flag 0.40.

## 1. Did the corrected failure estimate move between the two periods?

No. It was 0.48 in both. The fixed sampling seed drew the same 10 scenarios
in both periods, and the judge gave the same verdict on each of the 10 both
times (support-0004, support-0046, support-0056 and support-0185 flagged).
The other 40 scenarios do not enter the estimate.

## 2. Do the intervals support a conclusion, or is the result uncertain?

Uncertain. Both intervals span 0.00 to 1.00. With 10 random conversations
and a judge whose errors were measured on a 44-trace test set, each flagged
conversation moves the estimate by about 21 points.

## 3. What did the risk groups reveal that the random estimate did not?

Where the failure concentrates (raw judge verdicts, not corrected):

| Risk group | Before | After |
|---|---|---|
| Refund or cancellation made (`write_action`) | 6 of 7 | 7 of 7 |
| More than one user turn (`multi_turn`) | 2 of 2 | 2 of 2 |
| Policy lookup (`policy_lookup`) | 9 of 19 | 11 of 21 |
| Random conversations in no risk group | 1 of 5 | 1 of 4 |

Conversations where the agent acts or talks over several turns are flagged
almost every time; plain conversations rarely are.

## 4. What action should happen if the estimate crosses the threshold?

It crossed in both periods (0.48 against 0.30). A crossing starts error
analysis on the flagged traces: open them from the Langfuse dashboard,
confirm each by hand, and add the confirmed failures as new evaluation cases
in the Homework 6 suite. Start with the refund and cancellation group, where
nearly every trace was flagged. With an interval this wide, also raise the
random sample before trusting the estimate itself.

## Deviations from the handout

- **Model.** Muse Spark 1.3 (Meta Model API) is both the Cartwheel model and
  the judge model, as in Homeworks 3 to 6.
- **Comparison periods.** "Before" is the Homework 3 run. Where a scenario
  was attempted more than once, only the attempt its results file recorded is
  kept; the others are set aside and listed by the monitor (3 before:
  support-0101 twice, support-0103; 4 after: support-0073, support-0243,
  support-0225, support-0174, all dropped connections).
- **Changes before the "after" run.** `scenarios/runner.py` records the
  session id of every result, and the agent retries a dropped model
  connection in place (`num_retries: 3`). Prompts, tools and replies are
  unchanged.
- **Judge runner.** `monitoring/run_judges.py` uses the two settings the
  judge was measured with in Homework 5 (structured output, 24,000-token
  ceiling). The prompt, model and verdict parser are unchanged.
- **Background judge calls.** The corporate proxy on the course laptop cuts
  any connection silent for about 60 seconds, and on the grading days a judge
  answer took longer. `monitoring.run --background` sends the identical
  request through the endpoint's background mode (queue, then poll). A check
  on four already graded conversations matched 4 of 4.
- **Scheduled workflow.** `.github/workflows/monitor.yml` has not run on
  GitHub: the judge's key is an internal credential that may not be stored on
  a third-party CI service, and GitHub's runners cannot reach the internal
  endpoint. The same command ran locally instead
  (`--last-hours 24 --background`, 54 conversations, exit 0).
- **Corrected prevalence score.** Langfuse Cloud rejects a score with no
  trace attached, so the `unsolicited_content_corrected_prevalence` records
  were not stored. The verdict scores were. The corrected estimates live in
  `history.jsonl` and `prevalence.svg`.
- **Verdict score dates.** Each verdict score is dated at its conversation,
  not at the upload, so the dashboard's time axis separates the periods
  (before: 2026-09-15 evening, after: 2026-09-29 evening, US Central). The
  scores were first uploaded dated at upload time; re-dating them left a
  stale copy dated 2026-09-30, and Langfuse Cloud allows only 50 score
  deletions a day, so the copies could not all be removed. The dashboard's
  date range ends on 2026-09-29 to leave them out.
- **Dashboard "after" point includes five extra verdicts.** The local runs of
  the scheduled command graded some of the same "after" conversations that
  were not in the period's sample, and their scores land on the same
  evening. The dashboard's "after" point therefore shows 14 random verdicts
  (average 0.36) instead of 10 (0.40), and 27 risk verdicts (0.59) instead of
  26 (0.62). The numbers in this README and `history.jsonl` are the period's
  own sample. The "before" point matches exactly.
