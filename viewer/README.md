# Cartwheel trace viewer

A local, read-only viewer for the traces the Homework 2 endpoint writes to
Langfuse. FastAPI backend, plain HTML/CSS/JS front end, no build step, no
database, and nothing writes back to Langfuse.

## Run it

```bash
SSL_CERT_FILE=$HOME/.local/share/meta-ca-bundle.pem \
.venv/bin/python -m uvicorn viewer.app:app --port 8030
```

Open <http://localhost:8030>.

The certificate variable is needed on a Meta-managed machine to reach Langfuse
Cloud over TLS. Against a local Langfuse (`localhost:3000`) it can be omitted.
Credentials come from `.env` — the same `LANGFUSE_*` values the agent server
uses. Nothing is configured twice.

## What it shows

**Left**, every trace, newest first: role, span and tool counts, and whether
anything went wrong. The question asked is the row label, so the list is
scannable without opening anything. Four filters across the top — **Role**,
**Tool**, **Status** (clean or with problems) and **Prompt version** — which
combine.

Role and prompt version arrive with the trace list, so those two filter
instantly. Tool and Status need each trace's observations, so the viewer scans
them in the background, four at a time; the header shows `scanning 7/12` until
that finishes. An un-scanned trace is hidden by a Tool or Status filter rather
than shown as a maybe.

Prompt version is the one to reach for when comparing prompts. The hash covers
the prompt *template*, before the caller's role and ids are injected, so one
prompt has one hash across every user and a change in the filter really does
mean a change in the prompt.

Traces recorded before September 2026 carry per-user hashes from the earlier
scheme, so an old shopper run and an old merchant run of the same prompt show
up as two different versions. That is history, not a bug.

**Right**, one trace:

- **Problems**, listed first, before you have to go hunting. A trace with none
  says so explicitly rather than leaving you to infer it from absence.
- **The journey** — what the agent actually did, in order.
- **Raw observations**, collapsed — every span with its id, parent,
  timestamps, level, status message, model and tokens, plus the untouched
  JSON, and the whole trace as Langfuse returned it.

## The journey

A strip across the top maps the whole run in one line, each step sized by the
share of wall clock it took, so the shape is legible before you read anything.
Clicking a step jumps to it.

Below that, a timeline. Model calls are shown as steps alongside tool calls,
which matters: without them a run reads as "ask, tool, answer" when what
happened was *think, act, think again, answer* — and the thinking is where
effectively all the time goes. A five-tool run in this repo spends 285 seconds
in the model and 0.05 seconds in the tools. The header states that split.

Each step carries its offset from the start of the request, its own duration,
and a bar showing its share. Questions, answers and failures are expanded on
arrival; everything else opens on a click.

A model step also shows **what the model saw** — the full message list at that
point, including the system prompt and every earlier tool result. Watching
that list grow from 2 to 11 messages across a run is usually the fastest way
to understand why a late call went wrong.

Model output is `null` throughout: this model API does not report generation
content. The viewer says so in place, and does **not** count it as a problem —
it is an instrumentation gap, not an agent failure.

## What counts as a problem

Three independent sources, because any one of them alone misses real failures:

| source | catches |
|---|---|
| observation `level` is ERROR or WARNING | spans the SDK marked failed |
| `statusMessage` is set | the exception text, e.g. a dropped model connection |
| a tool result with `ok: false` or an `error` key | **a permission denial or a not-found** |

The third matters most. Cartwheel tools return a structured refusal rather than
raising, so a denied `get_order` is a *successful span carrying a failed
result*. A viewer that only read span status would call that run clean.

A root span with no output is also flagged: it means the run did not finish, so
there is no final reply to read.

## Design notes

Missing values are preserved and rendered as missing. A null tool result shows
as `not recorded (null)` in a dashed box, never as an empty string — "the tool
returned nothing" and "the tool returned an empty string" are different bugs,
and a viewer that renders them identically hides one of them.

This reads Langfuse directly on every request and caches only in the browser
tab. Reload always shows current data; there is no local copy to go stale.

`analysis/helpers/normalization.py` is *not* reused for the trace shape. That
normalizer feeds Module 2's error analysis and flattens away observation ids,
parent links, `level` and `statusMessage` — exactly the fields this viewer
exists to show. Its Langfuse configuration helpers are reused.
