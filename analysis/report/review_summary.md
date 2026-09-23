# Review summary (Homework 4)

Human trace review and failure taxonomy over the Cartwheel Module 1 trace
store. Every figure below is derived from the committed artifacts under
`analysis/state/` and can be regenerated from them.

## The reviewed sample

**108 distinct traces**, above the required 100, drawn from the 285
`support-*` traces produced by the Module 1 scenario run. Reviewing 108 covers
roughly 43% of the ~250 distinct scenarios in that store.

| Batch | Traces | Selection |
| --- | --- | --- |
| 1a | 30 | cluster representatives (`diversity`, k-means on six trace features) |
| 1b | 20 | uniform random |
| 2 | 30 | spread across one product dimension, `intent`, chosen before outcomes were inspected |
| 3 | 25 | depth searches for candidate modes and close negatives |
| 3b | 3 | depth search re-evidencing `missing_threshold_escalation` |
| **Total** | **108** | no trace counted toward more than one batch |

Roles: 85 shopper, 13 support, 10 merchant. Batch 2 spread 30 traces evenly
across all eight `intent` values (4 each of `return_refund`, `order_status`,
`policy_question`, `product_search`, `dispute`, `out_of_scope`; 3 each of
`cancellation`, `account_change`), so values holding 9 scenarios contributed
as many traces as values holding 68.

**Population definition.** The 285 `support-*` traces, deliberately excluding
53 `pilot-*` traces that are also Module 1 records. The pilots ran against
scenarios that were subsequently revised during Homework 3 review, so a pilot
failure can be an artifact of a scenario that was thrown away.

**Batch 1 needed a correction.** The `diversity` strategy is two thirds cluster
representatives and one third random, so the first 30 traces were 20 + 10
rather than the handout's 15 + 15. Five uniform random picks were added, giving
20 cluster representatives and 15 random. Over-delivering on cluster
representatives is harmless; being short on the only unbiased arm is not.

## Annotations

141 annotations across 106 of the 108 traces, including 26 marked "no failure
observed" and 3 promoted from accepted agent suggestions. Two traces
(`support-0044`, `support-0017`) were staged late as depth candidates and carry
labels but no free-text annotation.

Open coding followed the first-failure convention: read until the first
requirement violation, write a free-text note in the reviewer's own words, stop.
No predefined categories were used during the initial reading.

## Failure modes

Eight binary modes. Every mode has at least three confirmed positive traces,
at least three close negatives, an originating human annotation, and a
requirement source.

| Mode | Fail | Sample fraction | Positives | Close negatives | Evaluator | Requirement |
| --- | --- | --- | --- | --- | --- | --- |
| `redundant_tool_path` | 44 | 0.407 | 19 | 4 | code check | TOOL-10 † |
| `unsolicited_content` | 35 | 0.324 | 27 | 4 | LLM judge | RESP-6 † |
| `response_style` | 25 | 0.231 | 18 | 4 | LLM judge | RESP-5 |
| `ineffective_tool_strategy` | 20 | 0.185 | 16 | 4 | LLM judge | RESP-7 † |
| `search_tool_failure` | 16 | 0.148 | 8 | 4 | code check | TOOL-3, TOOL-11 † |
| `unsupported_claim` | 12 | 0.111 | 12 | 4 | LLM judge | RESP-1, RESP-2, RESP-3 |
| `missing_threshold_escalation` | 5 | 0.046 | 3 | 4 | code check | ESC-5 † |
| `unauthorized_refusal` | 5 | 0.046 | 5 | 3 | code check | AUTH-1, AUTH-2 † |

† Requirement added by this review; see the `SPEC.md` section below.

**These are sample fractions, not prevalence estimates.** Clustering, the
`intent` stratification, and the depth searches deliberately changed the
composition of the reviewed sample. Homework 5 will estimate prevalence over the
complete Module 1 trace store.

Definitions, boundaries, positive traces and originating annotation ids are in
`analysis/state/patterns.json`.

## Saturation: new modes in the final 15 traces

**Zero.**

| Batch | New modes |
| --- | --- |
| 1, cluster + random (35) | +7 |
| 2, intent (30) | +0 |
| 3, depth (25) | +1 (`missing_threshold_escalation`) |
| 4, final 15 | **+0** |

The final 15 uniformly sampled traces produced 20 annotations touching five
existing modes and no previously unseen one. The batch that preceded it *did*
produce a new mode, so the zero is evidence of saturation rather than of a
reviewer who had stopped looking.

## One taxonomy revision, in full

**`missing_store_policy_check` was proposed, searched, and withdrawn.**

Three annotations in batch 1 said some version of "check whether the store has
its own policy" — for example `support-0202`, "chcek if the store has its own
policy". These were grouped into a candidate mode defined as answering a returns
question from the platform policy without checking for a store override.

A depth search then retrieved 15 unreviewed scenarios whose tuple carried
`applicable_policy = store_override_looser` or `store_override_stricter`, of
which 17 of 18 never mentioned an override in the reply. Thirteen carried agent
suggestions proposing the mode.

**The search disconfirmed it.** All thirteen suggestions were dismissed, and
none of the 22 annotations written on those 15 traces mentions a store
override. The
Part C re-run supplied independent evidence: `support-0001` called
`get_policy("store-northwind-books-policy")` and cited it correctly — "Northwind
Books accepts returns within 45 days of delivery, instead of the Cartwheel
default of 30 days". The agent handles overrides.

The mode was retired and its three annotations folded into `unsupported_claim`,
whose definition now covers citing the platform window where a store override
applies. A search designed to confirm a mode killed it instead.

**A second revision** is worth recording because it changed the shape of the
taxonomy. While tightening ESC-5's definition, testing it against its own five
supporting traces showed only one fit: the other four ended with the agent
announcing a lookup and stopping, not with an unescalated high-value refund.
Those four notes moved into `ineffective_tool_strategy`, whose definition was
widened to "the turn ended with the tool loop unfinished". Three replacement
positives were searched for and one (`support-0031`, $121.00) was confirmed, so
`missing_threshold_escalation` retains the required three positives. Had the
definition not been tested against its own evidence, the mode would have been
labeled against 105 traces on a rule that four of its five examples did not
satisfy.

## Rejected search suggestions

26 suggestions were pushed across four depth searches: **3 accepted, 23
dismissed, none left pending**. Every decision persists in
`analysis/state/suggestions.json`.

Dismissals by mode: `missing_store_policy_check` 13, `unsolicited_content` 5,
`unauthorized_refusal` 3, `missing_threshold_escalation` 2.

The two most informative rejections define mode boundaries:

- **Three merchant refusals dismissed** as "no failure". AUTH-1 limits merchants
  to their own store, so the same refusal wording that is a failure from a
  support caller is correct from a merchant. `unauthorized_refusal` is scoped to
  callers whose role permits the action.
- **Four `account_change` escalation offers dismissed.** ESC-2 makes escalation
  mandatory for account changes, so the offer is required rather than
  unsolicited. `unsolicited_content` excludes escalations that sections ESC-1
  through ESC-5 require.

## Revisions to SPEC.md

Five of the eight modes originally had no requirement to cite. The
specification described what the agent must do and almost nothing about what it
must not volunteer, with RESP-5 carrying weight it was not written for. Six
requirements were added; each is recorded in `SPEC.md` section 7 with its
motivating annotation.

| Requirement | Mode | Motivating annotation |
| --- | --- | --- |
| RESP-6, answer scope | `unsolicited_content` | `a1789666433529181`, `support-0227`: "The customer did not ask for a refund, there is no reason to answer this" |
| ESC-5, high-value returns | `missing_threshold_escalation` | `a1789766457332`, `support-0001`: "should be escalating to human because it's over $100" |
| TOOL-10, one lookup per question | `redundant_tool_path` | `a1789759653422`, `support-0230`: "find_order and get_order are the same thing, but neither has compelte data" |
| RESP-7, tool query construction | `ineffective_tool_strategy` | `a1789755880533`, `support-0045`: "Searching by price is probably not a reliable way to find a user's order" |
| TOOL-11, optional parameters accept omission | `search_tool_failure` | Part C span analysis, trace `53c316f5ceec4910f64b1337801bd543` |
| AUTH-2, the model is told what its caller may do | `unauthorized_refusal` | `a1789765387826`, `support-0072`: "this is a support person who has access to orders this is an incorrect denial" |

**TOOL-10 and TOOL-11 are code requirements.** No prompt change satisfies
either, and an LLM judge is the wrong evaluator for a mode derived from them.
They belong in Module 3 as regression checks against corrected tools.

## A defect found during review

`search_products` fails on **129 of 162 calls across the 285-trace store, 80%**.
It is the most-called tool in the store. One root cause with two surfaces:
optional parameters cannot be omitted, so the model emits the string `"null"`
for `max_price_usd` and `store` and the schema rejects it
(`Invalid JSON input for tool search_products`), or substitutes `store: ""` and
gets `not_found: no store named ''`.

The Part C re-run reproduced it exactly: three calls, three failures, and the
third attempt repeated the identical call that had already failed. The retry
loop is a second defect stacked on the schema one, and the shopper-facing reply
("I wasn't able to pull a clear catalog match") conceals both.

This explains annotations that read as agent failures but are downstream of a
tool that could not have returned anything, including `support-0160` and
`support-0168`.

## How the labels were produced

864 judgments, 8 modes across 108 traces, no cell left undecided. Provenance is
recorded per label in `analysis/state/labels/<mode>.jsonl`.

| Source | Labels | Meaning |
| --- | --- | --- |
| `code_check` | 397 | derived deterministically from spans |
| `reviewer_default` | 236 | no note recorded during review and no mechanical signal |
| `human` | 198 | the reviewer's own judgment |
| `human+code` | 33 | reviewer's annotation and the code check agree |

**Limitation, stated plainly.** Open coding used the first-failure convention,
so a mode occurring late in a trace already annotated for something earlier was
not necessarily recorded. The 236 `reviewer_default` labels are Pass by absence
of evidence rather than by positive judgment. These counts are therefore closer
to a first-failure count than to an any-instance count, and the two differ. Where
a code check and a human annotation disagreed, the human label was kept and the
disagreement recorded rather than resolved silently; this happened twice.

Two annotations were retracted by the reviewer during labeling
(`support-0007`, `support-0019`, both asserting that `find_order` already
supplies refund eligibility). They are preserved under `retracted` in
`patterns.json` rather than deleted. The retraction was correct:
`find_order.refund_eligible` is a boolean stamped at seed time, while
`check_return_eligibility` computes the window live and returns the `policy_id`
that RESP-1 requires. They agree on 65 of 65 comparable pairs in this store, but
that is a snapshot coincidence rather than equivalence.

## Readiness for Homework 5

Homework 5 needs at least 30 Fail labels per mode to split and validate a judge.

| Mode | Fail | Evaluator | Status |
| --- | --- | --- | --- |
| `redundant_tool_path` | 44 | code check | no alignment study needed |
| `unsolicited_content` | 35 | LLM judge | **ready** |
| `response_style` | 25 | LLM judge | short by 5 |
| `ineffective_tool_strategy` | 20 | LLM judge | short by 10 |
| `search_tool_failure` | 16 | code check | no alignment study needed |
| `unsupported_claim` | 12 | LLM judge | **short by 18, collect early** |
| `missing_threshold_escalation` | 5 | code check | no alignment study needed |
| `unauthorized_refusal` | 5 | code check | no alignment study needed |

Only `unsolicited_content` is ready to support a judge today. The four
code-check modes need no alignment study, so their low counts do not block
anything. `unsupported_claim` is the real gap: at 12 Fail labels it is below the
handout's 15-label warning line and needs a targeted collection round early in
Homework 5. A retrieval route exists — compare the `policy_id` values cited in a
reply against those actually returned by `search_help_center` in the same trace.

Merging `unsupported_claim` into `response_style` would reach 34 on paper and
was considered and rejected: their Fail sets overlap on only 3 traces
(Jaccard 0.09), and a judge validated against two criteria at once cannot report
which failure moved.

## Reproducing these numbers

    # sample size -> 108
    jq 'length' analysis/state/sample_manifest.json

    # composition, one object -> {"diversity":30,"dimension:intent":30,"random":20,...}
    jq -c '[group_by(.strategy)[] | {(.[0].strategy): length}] | add' \
      analysis/state/sample_manifest.json

    # Fail count for one mode -> 35
    jq -s '[.[] | select(.superseded_by == null)] | map(select(.label == 1)) | length' \
      analysis/state/labels/unsolicited_content.jsonl

    # Fail count for every mode, one line each
    for f in analysis/state/labels/*.jsonl; do
      printf '%-32s %s\n' "$(basename "$f" .jsonl)" \
        "$(jq -s '[.[] | select(.superseded_by == null)] | map(select(.label == 1)) | length' "$f")"
    done

    # annotations -> 141
    jq 'length' analysis/state/annotations.json

    # dismissed suggestions -> 23
    jq '[.[] | select(.dismissed)] | length' analysis/state/suggestions.json
