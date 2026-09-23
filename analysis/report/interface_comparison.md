# Interface comparison (Homework 4, Part A)

The submitted review interface is `analysis/review_app/`: `conversation.py`,
`server.py`, and a single-file `ui/index.html`. Run it with

    .venv/bin/python -m analysis.review_app.server

The supplied `analysis/server.py` and `analysis/ui/index.html` were read as
references and are not the submission.

## Friction observed in the standard Langfuse annotation view

Eight traces were reviewed in the ordinary Langfuse view before any code was
written, including both turns of two multi-turn sessions.

**The trace view is inverted, not merely unordered.** For
`d013e07060fa8cf9d95de9bc06735255` (`support-0043` turn 1, 24 observations) the
final reply sits on the trace-level `output` field, which Langfuse renders at
the top of the page. The work sits three levels down —
`cartwheel.session_message > Agent Workflow > cartwheel-support.agent` — and
only then 10 `TOOL` spans interleaved with 11 `GENERATION` spans.

So the reader gets the conclusion first and the evidence last, behind a tree
they must expand. For error analysis that is worse than inconvenient: open
coding asks for the *first* failure, "the earliest step to violate the product
requirements", and a view that shows the final answer first invites the reviewer
to form a verdict and then hunt for support. On this trace the agent made ten
tool calls and then said it could not find the order.

**`openai.response` spans hide the model's replies.** `output` is null on all
11 generations; the span records what was sent to the model, never what came
back. A reply therefore survives only inside the *next* generation's `input`, so
reading step 3's response means opening span 4 and scrolling. Every input is the
full cumulative history with the 1,440-character system prompt at index 0 —
15,840 characters of identical text in one trace — so every span preview looks
the same, because the only part visible without expanding *is* the same.

**Sessions are invisible.** Langfuse's native `sessionId` is null on all 359
traces; session identity lives in
`metadata.attributes["cartwheel.session_id"]`. Langfuse does not know the
conversations are conversations. Abandoned retry attempts from dropped
connections also sit alongside real turns with nothing marking them dead:
`e5354c5313a182f3262d36f35ea9d2f4` is a third `support-0043` trace that is not
part of the conversation.

## One design retained from the reference interface

**The file-backed API contract.** One state file per endpoint, `GET` reads,
`POST` overwrites, atomic write via temp-and-replace, permissive CORS for a
local single-user tool. `/api/annotations`, `/api/patterns` and
`/api/suggestions` keep the reference paths and payload shapes.

This was kept deliberately rather than by default. The error-analysis skill's
live loop specifies an agent that polls `state/annotations.json` every two
seconds and pushes groupings to `POST /api/patterns`; preserving the contract
meant that tooling worked against the new interface unchanged. The inline
annotation interaction was also retained: select text, a popover appears, Enter
saves, and the span is wrapped in a temporary highlight before focus moves so
the reviewer can still see what they are annotating once the browser clears the
native selection.

## One design changed after inspecting the traces

**The span tree was replaced by a single chronological spine, rebuilt from one
span.**

Because each generation's input is cumulative, *the last generation's input is
the entire conversation* — confirmed on `support-0043`, where turn 1's last
generation carries 22 messages and turn 2's carries 30, spanning both user
turns. `conversation.py` reads that one span and renders:

    USER -> STEP 1..n (reasoning + tool call + result) -> FINAL REPLY

The three wrapper spans are flattened away, the system prompt collapses to one
muted line in the header, and the final reply is placed last. Nothing needs
expanding to be read.

This also recovers content the alternatives lose. The shared
`analysis.helpers.normalization` builds assistant messages from generation
`output`, which is null here, so it drops every intermediate reasoning
statement: a trace rendered from it shows ten tool calls with no statement of
why any of them happened. The new renderer recovers reasoning on **85 of 88
steps** in the first batch. That matters because the system prompt requires the
agent to explain its reasoning in plain text before every tool call, so the
omission was hiding a directly checkable requirement.

Three smaller changes came from the same inspection:

- **Tool results render inline rather than collapsed.** The skill suggests
  collapsing long results by default; the measured median tool output is 275
  characters, p90 is 1,125. Collapsing everything would have hidden the evidence
  to save nothing, so only genuine outliers above 1,400 characters fold away.
- **Retrieval renders as a table** of `policy_id`, score and snippet rather than
  JSON, because RESP-1 citations are checked against exactly those ids.
- **Outlier badges are computed over the whole 285-trace store, not the loaded
  batch.** Computed over a 30-trace batch the same rule flagged 14 of 30, which
  is not a signal. Store-wide and top-decile-only it flags 7 of 30. Token counts
  are never flagged: Langfuse records zero tokens for every Cartwheel span, so
  the normalizer substitutes a word count, and a badge reading "76 tokens" would
  state a number the trace store does not hold.

## One limitation remaining

**The structured labeling view does not scale to the labeling pass it exists
for.** Eight modes across 108 traces is 864 judgments, and the view renders them
as a grid of two-button cells. Nothing supports bulk application, keyboard
navigation, or applying a value down a column.

In practice most labels were therefore written by scripts directly into
`analysis/state/labels/<mode>.jsonl`: 397 from deterministic code checks, 236 as
`reviewer_default`, and 198 pre-filled from open-coding annotations, leaving 27
genuinely open cells for the reviewer to click. Adding a "show only rows with an
undecided cell" filter mid-assignment made those 27 findable — before it, the
view showed 108 rows by 8 modes with no way to locate what still needed a
decision. The filter made the last 27 tractable; it did not make the first 837
so.

The honest statement is that the interface's trace view carried the assignment
and its labeling view did not. A version that earned its place would offer
column-wise apply, a filter by mode and by current value, and a keyboard path
through undecided cells.

A second, narrower limitation: annotations anchor to a block key plus their
quoted text, and highlights are matched by locating that text at render time.
This survives renderer changes, which it had to twice, but a quote appearing
more than once in the same block highlights only the first occurrence.

Not built: the map view described in the skill. It is not among the handout's
required features and would need a `graph.json` projection that does not exist.
