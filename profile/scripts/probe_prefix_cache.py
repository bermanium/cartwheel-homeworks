"""Probe how much of the agent's request the LLM API reuses between users.

The Part C runs measured caching over whole conversations, where the number
also depends on conversation length, which server a request lands on, and
what else the server handled. This probe isolates the prompt order. For each
template (before and after the Session context move) it sends, back to back:

1. user A asks a question,
2. user B (same role, different user id) asks the same question: the cached
   tokens are the prefix two different users share,
3. user A asks again, word for word: if even this is not cached, requests
   are not reaching a server that still holds the prefix.

Each request carries the agent's real tool definitions and rendered system
prompt, as the Agents SDK sends them through LiteLLM, and asks for at most
16 output tokens: the prompt is processed (and cached) in full either way.

    .venv/bin/python profile/scripts/probe_prefix_cache.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

MODEL = "openai/rl-muse-spark-1-3-sglang-playground"
BEFORE_COMMIT = "2b12db1"  # last commit with Session context near the top
ROLE = "shopper"
USER_A, USER_B = 101, 202
QUESTION = "What is your return policy?"
REPEATS = 2
OUT_PATH = REPO_ROOT / "profile" / "results" / "prefix_cache_probe.json"


def template_at(commit: str) -> str:
    source = subprocess.run(
        ["git", "show", f"{commit}:agent/agent.py"],
        cwd=REPO_ROOT, capture_output=True, text=True, check=True,
    ).stdout
    start = source.index('SYSTEM_PROMPT_TEMPLATE = """')
    end = source.index('"""', start + len('SYSTEM_PROMPT_TEMPLATE = """')) + 3
    namespace: dict = {}
    exec(source[start:end], namespace)
    return namespace["SYSTEM_PROMPT_TEMPLATE"]


def chat_tools(role: str) -> list[dict]:
    from agent.agent import TOOLS_BY_ROLE

    return [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.params_json_schema,
            },
        }
        for tool in TOOLS_BY_ROLE[role]
    ]


def main() -> int:
    from dotenv import load_dotenv

    load_dotenv(REPO_ROOT / ".env")
    import litellm

    from agent.agent import SYSTEM_PROMPT_TEMPLATE, render_system_prompt
    from agent.auth import AuthContext

    templates = {"before": template_at(BEFORE_COMMIT), "after": SYSTEM_PROMPT_TEMPLATE}
    tools = chat_tools(ROLE)
    rows = []
    for repeat in range(1, REPEATS + 1):
        for order, template in templates.items():
            for step, user_id in (("user A", USER_A), ("user B", USER_B), ("user A again", USER_A)):
                ctx = AuthContext(user_id=user_id, role=ROLE, store_id=None)
                response = litellm.completion(
                    model=MODEL,
                    messages=[
                        {"role": "system", "content": render_system_prompt(ctx, template)},
                        {"role": "user", "content": QUESTION},
                    ],
                    tools=tools,
                    max_tokens=16,
                    num_retries=3,
                    timeout=50,
                )
                usage = response.usage
                details = getattr(usage, "prompt_tokens_details", None)
                row = {
                    "repeat": repeat,
                    "order": order,
                    "step": step,
                    "prompt_tokens": usage.prompt_tokens,
                    "cached_tokens": int(getattr(details, "cached_tokens", 0) or 0),
                    "output_tokens": usage.completion_tokens,
                }
                rows.append(row)
                print(json.dumps(row), flush=True)
                time.sleep(1)
    OUT_PATH.write_text(json.dumps({"model": MODEL, "role": ROLE, "rows": rows}, indent=1))
    print(f"saved {OUT_PATH.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
