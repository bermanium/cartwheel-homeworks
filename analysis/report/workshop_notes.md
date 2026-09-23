# Workshop notes (Homework 4, Part C)

Execution-level inspection of nine fresh Cartwheel runs, to find failure modes
the open-coding pass may have missed.

Every observation below is a **hypothesis for the reviewer to accept, revise or
reject**, not a label. None of it was written into the taxonomy directly.

## Substitution: Raindrop Workshop could not run on this machine

Workshop was installed successfully and then refused to execute.

| Step | Result |
|---|---|
| `bash install.sh --no-setup` | binary downloaded and sha256-verified, 68 MB at `~/.raindrop/bin/raindrop` |
| `raindrop --version` | **exit 137 (SIGKILL)**, no output, on three consecutive runs |
| `codesign -dv` | `Signature=adhoc` — self-signed, no Developer ID |
| `spctl -a -vv` | `rejected` |
| `xattr -l` | no quarantine attribute, so this is policy enforcement, not a Gatekeeper prompt |

The machine's endpoint security kills adhoc-signed downloaded binaries. This is
the same failure `uv` shows throughout Modules 1 and 2. The contributors'
build-from-source path needs `bun`, and there is no `bun`, `node`, `npm` or
Homebrew available; `bun` ships as the same kind of binary, so it would very
likely be killed too. No route exists here without administrator or MDM
approval.

`curl -fsSL https://raindrop.sh/install | bash` also fails earlier with a bash
syntax error, because the script desynchronises when read from a pipe. Running
the downloaded file works. That is incidental to the block above.

**What was done instead.** Part C's purpose is execution-level access to model
activity and tool calls. The Cartwheel OpenTelemetry spans already carry every
raw tool call, its arguments, and its result, so the same inspection was
performed directly against the spans of nine freshly executed runs. What is
lost relative to Workshop is the live UI and its agent-facing query interface,
not the underlying evidence.

## The nine runs

Executed 2026-09-18 against `openai/rl-muse-spark-1-3-sglang-playground`
through the Meta Model API, via `scenarios/runner.py --concurrency 1`.
Results: `scenarios/results/workshop-run.jsonl`. Identifiers are Langfuse trace
ids, standing in for Workshop run ids.

| Scenario | Role | Intent | Langfuse trace id | Tool calls |
|---|---|---|---|---|
| support-0001 | shopper | return_refund | `d3954f532043a812a382765433131433` | find_order, check_return_eligibility, get_policy, get_order |
| support-0047 | merchant | order_status | `e03172b1ec6492b1de8f49d06fc9c6fe` | get_order |
| support-0052 | shopper | account_change | `45ad9bc81913fddc2d17d7258e33b294` | escalate_to_human |
| support-0072 | support | dispute | `9dc39ce6342f6692a8660fe6320ff62c` | search_help_center |
| support-0092 | shopper | product_search | `c9241b2bbe57b79a5749c11711319588` | find_order, get_order |
| support-0093 | shopper | product_search | `53c316f5ceec4910f64b1337801bd543` | search_products ×3 |
| support-0107 | shopper | out_of_scope | `f86e05ed39f31c30c010ae5da6b264f4` | none |
| support-0121 | shopper | policy_question | `78f6f141bfcdf69839148011403c33d3` | search_help_center ×2, get_policy ×3, escalate_to_human |
| support-0241 | support | order_status | `f005eafd2f1c1f2510cbc064977a6e85` | get_order, check_return_eligibility |

Coverage: three roles, seven intents, read tools, both write tools that remain
reachable, and one run with no tool call at all.

**Cancellation could not be covered.** No `cancellation` scenario still has an
order in `placed` state; the Module 1 run cancelled all of them. Reseeding
would have destroyed the committed database, so the batch is nine runs rather
than ten. Worth recording on its own: **the Module 1 trace store is not
reproducible without a reseed**, because its write scenarios mutated the
records they depend on.

## Candidate findings

### 1. `search_products` fails on every call, and the agent retries unchanged

`support-0093` called `search_products` three times and all three failed.

```
1. {"limit":5,"max_price_usd":"null","query":"Heavy-Duty Vase","store":"null"}
   -> Invalid JSON input for tool search_products
2. {"limit":5,"max_price_usd":1000000,"query":"Heavy-Duty Vase","store":""}
   -> {'ok': False, 'error': 'not_found', 'reason': "no store named ''"}
3. {"limit":5,"max_price_usd":"null","query":"Heavy-Duty Vase","store":"null"}
   -> Invalid JSON input for tool search_products
```

The model has no way to omit an optional parameter, so it emits the **string**
`"null"`, which the schema rejects. On the second attempt it substitutes an
empty `store`, which then fails a different way. On the third it returns to the
exact call that already failed.

This reproduces the store-wide measurement taken during review: **129 of 162
`search_products` calls fail across the 285 Module 1 traces, 80%.** It is the
most-called tool in the store.

Two distinct defects are stacked here, and the second is the one open coding
could not see: the schema rejects the placeholder, **and** the retry policy
repeats an identical failing call rather than adapting. The reply that reaches
the shopper ("I wasn't able to pull a clear catalog match") looks like a
reasonable limitation and hides both.

Relates to the `search_tool_failure` mode. Suggested as evidence that the mode
is a code defect rather than an agent behaviour.

### 2. The unauthorized refusal does not depend on what the tool layer says

This is the finding the execution view produced that the reply text cannot.

`support-0072`, a **support** caller asking about a disputed charge. The agent
made exactly one tool call, `search_help_center`, and its own reasoning states
the intent:

> "I'll search the help center for our disputed-charge policy so I can share
> general guidance **without pulling up** [the order]"

It then replied "I can't pull up another customer's order details, even for a
support request." **It never called `get_order` at all.** The refusal is an
assumption about its own authority, not a response to anything the tool layer
said.

Contrast `support-0047`, a **merchant** asking about an order from another
store. That run *did* call `get_order`, the tool returned
`cartwheel.permission_denied=true`, and the reply explained the limit
correctly. Authorization worked exactly as AUTH-1 specifies.

So the two runs differ in execution even though the replies read almost
identically: one tested its authorization and reported the answer, the other
never tested it.

**The Module 1 trace for the same scenario fails a third way, and it is the
most damning of the three.** Trace `5ccfc003a1415c35dc441bae7eed98b9` *did*
call `find_order("Sturdy Novel")`. The tool returned `ok: true` with the order
record and no permission denial — the authorization layer allowed it. The agent
then replied "I can't share another shopper's order details, even when
requested on their behalf."

So across two executions of one scenario the agent refused after a successful
lookup, and refused without attempting one. The refusal does not depend on what
the tool layer says, which is the clearest available evidence that it is a
belief the model holds about its own authority rather than an error-handling
defect. AUTH-2 addresses the belief; no change to the tool layer would.

If accepted, this sharpens `unauthorized_refusal` and changes its fix. It is
not error handling — it is the agent's model of what a support caller may do.
The system prompt's session-context block states the role but never states that
support may view any order. A prompt change would address it; a code change
would not.

### 3. Redundant tool pairs reproduce live

`support-0092` called `find_order` then `get_order` on the same order.
`support-0121` called `search_help_center` twice and `get_policy` three times to
answer one policy question. Both match `redundant_tool_path`, and both occurred
in runs executed after the taxonomy was drafted.

### 4. `response_style` reproduces live

`support-0052` opens "I completely understand how frustrating that is to juggle
two logins." The escalation itself is correct — ESC-2 makes it mandatory for an
account change — so this run is a positive for `response_style` and a close
negative for `unsolicited_content` in the same trace.

### 5. Evidence supporting a taxonomy decision already taken

`support-0001` called `get_policy` for `store-northwind-books-policy` and cited
it correctly: "Northwind Books accepts returns within 45 days of delivery,
instead of the Cartwheel default of 30 days."

The agent handles store overrides correctly here. This is independent support
for dropping `missing_store_policy_check`, which a targeted depth search had
already failed to confirm.

## The uncertain case

**`support-0001`, and whether the missing escalation is a failure at all.**

Order 1082 totals **$104.25**, above the `$100`
`refund_auto_approve_threshold_usd`. The reviewer's note on the Module 1 trace
for this scenario reads "should be escalating to human because it's over $100",
and the mode `missing_threshold_escalation` was built partly on it.

The fresh run made no escalation and no refund. Instead it ended:

> "Do you want me to go ahead and process the return for $104.25? If so, let me
> know the reason ... and I'll start it for you."

I can argue this two ways and cannot settle it from the trace.

- **It is a failure.** The amount is known, it is above the threshold, and
  ESC-1 says such cases always go to a human. Asking the shopper to confirm
  implies the agent can "start it" itself, which it cannot at this amount. The
  reply sets an expectation the next turn will have to walk back.
- **It is not a failure.** ESC-1 governs *refunds above the threshold*, and no
  refund was requested or issued. The shopper asked whether the item was
  returnable; the agent answered correctly, cited the right store policy, and
  asked for the information it needs before acting. Escalating before the
  shopper has asked for a refund would arguably be `unsolicited_content` — a
  mode the reviewer confirmed 28 times.

The distinction that decides it is whether `missing_threshold_escalation` fires
on the *turn where the refund is requested* or on *any turn where an
above-threshold amount is in scope*. The mode's definition does not currently
say, and both readings are consistent with the five notes behind it. This needs
the reviewer's decision, and the definition should be tightened either way
before the mode is labeled in Part E.

## Disposition

Reviewer decisions, recorded 2026-09-22. Every finding below was inspected
against its trace before being accepted.

| Finding | Mode | Disposition | What it changed |
|---|---|---|---|
| 1. `search_products` placeholder + unchanged retry | `search_tool_failure` | **accepted** | Added **TOOL-11** to `SPEC.md`: optional parameters must accept omission. Fixed the mode's requirement source and established it as a code defect, not an agent behaviour. |
| 2. Refusal is independent of the tool layer's answer | `unauthorized_refusal` | **accepted** | Added **AUTH-2** to `SPEC.md`: the model is told what its caller may do, and must not refuse on its own authority where the tool layer would allow the call. Changed the mode's fix from error handling to a prompt change. |
| 3. Redundant tool pairs reproduce | `redundant_tool_path` | **accepted** | Confirmed the mode in runs executed *after* the taxonomy was drafted. Supported **TOOL-10**. |
| 4. Empathy opener with a correct escalation | `response_style` | **accepted** | `support-0052` is a `response_style` **Fail** and an `unsolicited_content` **Pass** in the same trace: the opener is the failure, the ESC-2 escalation is required. Committed labels match. |
| 5. Store override handled correctly | (supports dropping `missing_store_policy_check`) | **accepted** | Independent evidence for retiring the mode, alongside a depth search that produced zero confirming positives. |
| Uncertain: escalation on an unrequested refund | `missing_threshold_escalation` | **revised** | Resolved in favour of the broad reading: ESC-5 fires on the turn the agent implies it can complete the request, not only where a refund was explicitly requested. The narrow reading would have deleted four of the mode's five examples. The definition was rewritten and three replacement positives were searched for, of which `support-0031` ($121.00) was confirmed. |

No Workshop finding was rejected. The one that came closest was the uncertain
case, which was revised rather than dropped; rejecting it would have retired
`missing_threshold_escalation`, and the reviewer judged unescalated high-value
returns the more important failure to keep tracking.

Rejected suggestions from the *depth searches* are a separate record: 11
dismissed, in `analysis/state/suggestions.json`, and discussed in
`review_summary.md`.
