#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from minisweagent.agents.default import DefaultAgent
from minisweagent.environments.local import LocalEnvironment
from minisweagent.models import get_model


OBSERVATION_TEMPLATE = """{%- if output.output | length < 10000 -%}
{
  "returncode": {{ output.returncode }},
  "output": {{ output.output | tojson }}
  {%- if output.exception_info %}, "exception_info": {{ output.exception_info | tojson }}{% endif %}
}
{%- else -%}
{
  "returncode": {{ output.returncode }},
  "output_head": {{ output.output[:5000] | tojson }},
  "output_tail": {{ output.output[-5000:] | tojson }},
  "elided_chars": {{ output.output | length - 10000 }},
  "warning": "Output too long."
  {%- if output.exception_info %}, "exception_info": {{ output.exception_info | tojson }}{% endif %}
}
{%- endif -%}
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--system-prompt", required=True)
    parser.add_argument("--user-prompt", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--model-class", default="")
    parser.add_argument("--provider", default="")
    parser.add_argument("--cwd", required=True)
    parser.add_argument("--step-limit", type=int, default=0)
    parser.add_argument("--command-timeout", type=int, default=600)
    args = parser.parse_args()

    system_prompt = Path(args.system_prompt).read_text()
    user_prompt = Path(args.user_prompt).read_text()

    model_config = {
        "model_name": args.model,
        "model_kwargs": {
            "drop_params": True,
            "temperature": 0.0,
            "parallel_tool_calls": True,
        },
        "cost_tracking": "ignore_errors",
        "observation_template": OBSERVATION_TEMPLATE,
        "format_error_template": (
            "Tool call error:\n<error>{{ error }}</error>\n"
            "Every response must include at least one bash tool call. "
            "When finished, run exactly: echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"
        ),
    }
    if args.model_class:
        model_config["model_class"] = args.model_class
    if args.provider:
        model_config["provider"] = args.provider

    model = get_model(config=model_config)
    env = LocalEnvironment(
        cwd=args.cwd,
        timeout=args.command_timeout,
        env={
            "PAGER": "cat",
            "MANPAGER": "cat",
            "LESS": "-R",
            "PIP_PROGRESS_BAR": "off",
            "TQDM_DISABLE": "1",
        },
    )
    agent = DefaultAgent(
        model,
        env,
        system_template=system_prompt,
        instance_template="{{ task }}",
        step_limit=args.step_limit,
        cost_limit=0,
        output_path=Path(args.output),
    )
    info = agent.run(user_prompt)
    print(json.dumps(info, indent=2))
    return 0 if info.get("exit_status") == "Submitted" else 1


if __name__ == "__main__":
    raise SystemExit(main())
