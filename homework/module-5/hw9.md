# Homework 9, reducing cost

## Work through the assignment with a coding agent

Paste this prompt at the start of a coding agent session in your repository:

> Walk me through Homework 9 in `homework/module-5/hw9.md` as an interactive tutorial. Read `AGENTS.md`, the handout, the files in `optimize/`, and my Homework 5 and Homework 8 artifacts first.
>
> I am driving, and the goal is for me to learn how to reduce an agent's cost, not for you to finish the homework for me. Work one step at a time, in the handout's order. Before each step, explain in plain language what the step is for and what you expect it to show, then propose the command or change and wait for me to say go. Do not run a command, change a file, or generate anything until I have said so. Reading files to prepare a proposal is fine. After each step, show me the result and ask me what I think it means before you explain it. Keep questions few and short.
>
> Leave the decisions to me: which change to make in Part B, whether to keep or revert each change, the cascade threshold and decision, which configuration I would use, and the video. When I ask what to do, lay out the options and their tradeoffs instead of choosing for me.
>
> Concepts I need to understand before we use them: why customer conversation cost and judge cost are reported separately, why fewer input tokens do not always mean lower cost, what a cached prefix is and why the order of the prompt matters, what a model cascade is and how a threshold trades agreement for cost, and what makes a configuration dominated. Diagrams that would help me: the parts of one model request in the order the LLM API receives them, and the cascade's path from the proxy to the oracle.
>
> Never edit `eval_cases/`, `tests/`, `analysis/state/judges/`, or `optimize/state/`. Before any paid run, show me the model, the number of evaluated case runs, and the number of judge calls, then wait for my approval. Never print or commit secret values. If something fails, read the error, explain it plainly, and propose a focused fix.

Homework 9 asks you to lower the cost of the Cartwheel agent without lowering its development score.

You will first find which model calls cost the most. You will then try three ways to lower cost: making the agent use fewer tokens, reordering the prompt so that the LLM API can cache part of it, and running a less expensive model before a judge. Finally, you will compare the configurations you tried by score and cost.

## Expected work

- Estimated time: 3 to 5 hours.
- Required model access: your Homework 8 development model, one OpenAI model for Part C, and one less expensive model for Part D.

## Preparation

Run every command from the repository root. Pull the latest starter code, because `optimize.runner` now also reports cached tokens:

```bash
git pull
uv sync
```

Confirm that your Homework 8 results are present:

```bash
test -f optimize/state/final_version.json
```

In this handout, your **Homework 8 final run** is the development result saved with `--candidate final` in `optimize/results/`. If you did not finish Homework 8, follow the Preparation section of `hw8.md` to create the case split, including the reference cases patch if you have fewer than six cases. Then run the unchanged agent once, and use that run wherever this handout says Homework 8 final run:

```bash
uv run python -m optimize.runner --split development --candidate starting
```

Use only development cases in this homework. You already used the test cases in Homework 8.

Every run in this homework uses `optimize.runner`, which reports the development score, `write_pass_5`, input tokens, cached input tokens, output tokens, cost per 100 conversations, and median latency. Before you use a model for the first time, add its prices to `prices_per_million_tokens_usd` in `optimize/config.json`. Then test it with one request, e.g., "What is your return policy?", and confirm that `--debug` prints a tool call:

```bash
uv run python -m agent.cli --model MODEL_NAME --debug
```

A model can be listed by a provider and still fail, e.g., when no server is available or your account has no credits. One request finds the problem before you pay for a full run.

## Part A, find where cost comes from

Divide model calls into two groups:

- **Customer conversation calls** answer a user. Their cost grows with the number of users.
- **Judge calls** score responses in CI, monitoring, or analysis. Their cost grows with how often you evaluate.

A **call site** is the file and line that makes a model call. Using your exported Langfuse traces, e.g., `traces/support_traces.json` from Homework 3, and your Homework 5 and Homework 7 judge runs, add up the cost of each call site. Create `profile/results/costs.csv` with one row per call site:

```text
cost_category,call_site,purpose,model,calls,input_tokens,output_tokens,cost_usd
```

Use `customer_conversation` or `judge` in `cost_category`. Use the token counts returned by the LLM API. If a judge run did not save token counts, estimate them and say so in `purpose`. Save the script that creates the table in `profile/scripts/`.

## Part B, make the agent use fewer tokens

Every model call is billed by the tokens it reads and writes. Look at a few traces from your most expensive call site and find one place where the agent sends or receives more text than it needs. For example:

- a tool returns many fields, and the agent uses only a few of them,
- the policy search returns more documents than the answer needs, or
- the agent's replies repeat information the user already has.

Make one change that removes the extra text, e.g., return fewer fields from `get_order` in `agent/tools.py`. Commit the change, then run the development cases with your Homework 8 development model:

```bash
uv run python -m optimize.runner --split development --candidate fewer-tokens
```

Fewer input tokens do not always lower the cost. With a shorter tool result, e.g., the model may call another tool or write a longer reply. Compare the run with your Homework 8 final run on all of these numbers: development score, `write_pass_5`, input tokens, output tokens, cost, and latency.

Keep the change if it lowered cost or latency without lowering the development score or `write_pass_5`. Otherwise, revert it.

## Part C, reorder the prompt for caching

When many requests begin with the same text, the LLM API can reuse its work on that text and charge less for it. The shared beginning of a request is called the **prefix**, and the reused input tokens are called **cached tokens**. The prefix must match exactly, so a value that differs between users, such as a user id, ends it.

The Agents SDK sends each request in three parts:

1. The tool definitions.
2. The system prompt, rendered from `SYSTEM_PROMPT_TEMPLATE`.
3. The conversation messages.

The tool definitions are the same for every user with the same role. In the starter prompt, the `## Session context` block, with the role, user id, and store id, is near the top of the system prompt. The system prompts of two different users therefore match only in their first two lines.

OpenAI models cache automatically once the prefix is at least 1,024 tokens. Other providers have their own rules, e.g., Anthropic models cache only at markers that the Cartwheel agent does not set. Use an OpenAI model for Part C, and add its cached input price as `cached_input` beside its other prices in `optimize/config.json`, so that the runner bills cached tokens at the lower price.

Run the development cases with the current prompt:

```bash
uv run python -m optimize.runner --split development --candidate cache-before --model OPENAI_MODEL
```

Then move the `## Session context` block to the end of `SYSTEM_PROMPT_TEMPLATE` in `agent/agent.py`, without changing any wording. Commit the change and run the same cases again:

```bash
uv run python -m optimize.runner --split development --candidate cache-after --model OPENAI_MODEL
```

Compare `cached_fraction` (cached input tokens divided by all input tokens), cost, and the development score between the two runs. Keep the new order if the development score did not drop.

Zero cached tokens is an acceptable result. If you get zero, give the likely reason, e.g., the shared prefix was shorter than 1,024 tokens, or the runs paused long enough for the LLM API to clear its cache.

## Part D, test a model cascade on one judge

A **model cascade** runs a less expensive model, the **proxy**, first. When the proxy is unsure, a more expensive model, the **oracle**, decides instead. If most examples are easy, the proxy handles most of them and the total cost goes down.

In this homework, the oracle is one of your Homework 5 judges. "Oracle" means the judge you compare against, not a judge that is always right. Its verdicts on your Homework 5 labels are already saved in `analysis/state/judges/`, so you do not need to run it again.

If none of your Homework 5 judges was good enough to use, still complete Part D with your best judge. A cascade copies its oracle's mistakes, so in Step 3, also measure agreement with your human labels, and say that the cascade is not ready for CI or monitoring. If you did not build a judge, use the reference `unsupported_policy_claim` judge from the starter.

### 1. Set up the proxy

Choose a less expensive model as the proxy. Give it the same judge prompt, plus one instruction asking it to state its confidence in its verdict as a number from 0 to 1. Save the proxy's code in `cascade/run_proxy.py`.

### 2. Choose a threshold

The cascade keeps the proxy's verdict when the proxy's confidence is at or above a **threshold**, and uses the oracle's verdict otherwise. **Agreement** is the share of examples where the cascade and the oracle give the same verdict.

Before you calculate anything, decide the lowest agreement you will accept, e.g., 0.95. Run the proxy on your Homework 5 development labels. Try several thresholds, and for each one calculate the agreement and the share of examples the proxy handles. Save the results in `cascade/results/thresholds.csv`:

```text
threshold,agreement_with_oracle,agreement_with_human_labels,proxy_share,cost_per_1000_verdicts_usd
```

Choose the lowest threshold that meets your agreement requirement. A lower threshold lets the proxy handle more examples.

### 3. Check the threshold once

Run the cascade with the chosen threshold on your Homework 5 test labels. Do not change the threshold after you see the result. Save the agreement, the proxy's share, and the cost per 1,000 verdicts in `cascade/results/check.json`.

If the agreement is below your requirement, reject the cascade. A rejected cascade can earn full credit. The test set is small, so one or two disagreements change the agreement a lot. Report the number of disagreements as well as the rate.

## Part E, compare the configurations

Each run in Parts B and C is an agent configuration with a score and a cost. Create `profile/results/frontier.csv` with one row for your Homework 8 final run and one row for each run in this homework:

```text
candidate,model,development_score,write_pass_5,cost_per_100_conversations_usd,median_latency_seconds,frontier_status
```

As in Homework 8, a configuration is `dominated` when another configuration has an equal or higher score and an equal or lower cost, and is better on at least one of the two. Mark every other configuration `frontier`. `write_pass_5` and latency do not change the status, but they can rule out a configuration, e.g., refunds need a high `write_pass_5`.

## Video

Record one continuous screen video of no more than 5 minutes. In the video:

- Show the most expensive customer conversation call site and the most expensive judge call site.
- Explain your Part B change, and which numbers moved.
- Show how the reorder changed the cached fraction, and explain the result, even if it is zero.
- Explain your cascade threshold, and say whether the test agreement met your requirement.
- Show your frontier, and say which configuration you would use and why.
- Recalculate one number from a saved result.

## Optional extensions

**Try a new model.** Run your best configuration with a model you have not used yet, add it to the frontier, and check whether some prompt instructions are no longer needed. Newer models often do things that an older model had to be told.

**Estimate tokens by part of the request.** The LLM API reports only total input tokens. Count the tokens in the system prompt, the tool definitions, and the messages with the model's tokenizer, e.g., `tiktoken`, and scale the counts to match the reported total. Compare the size of the tool definitions with the size of the system prompt.
