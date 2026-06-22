#!/usr/bin/env python3
"""Validation orchestrator for user-story-based verification.

Stage 1:   A validation agent generates test scripts from the validation spec.
Stage 1.5: Smoke test — run first case of each story.
Stage 1.6: LLM review of generated scripts + smoke output.
Stage 1.7: Show review + smoke errors to the validation agent for fixing.
Stage 2:   Execute all stories via their drivers, verify outputs.
Stage 2.5: Final LLM review — correctness + collusion/cheating check.
"""

from __future__ import annotations

import json
import math
import os
import shlex
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib  # type: ignore


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Fixed paths inside the Docker container.
REPO_DIR = Path(f"/repo/{os.environ.get('REPO_NAME', '')}")
TESTS_DIR = Path("/tests")
VALIDATE_DIR = TESTS_DIR / "validate"
LOGS_DIR = Path("/logs/verifier")
SCRIPTS_DIR = LOGS_DIR / "validation_scripts"
VALIDATION_SPEC = VALIDATE_DIR / "validation_spec.toml"
PROMPT_TEMPLATE = VALIDATE_DIR / "validation_prompt.md.j2"
AGENT_PATCH = LOGS_DIR / "agent.patch"

# Retry budget for self-healing (hard caps — not configurable per task)
RETRY_MAX_TURNS = 50
RETRY_TIMEOUT_SEC = 600
MINISWEBENCH_STEP_LIMIT_MULTIPLIER = 2.5


def _normalize_validation_agent_harness(harness: str) -> str:
    """Normalize env-facing harness names to internal identifiers."""
    normalized = harness.strip().lower()
    if normalized not in {"claude_code", "miniswebench"}:
        raise RuntimeError(f"Unsupported FORCE_VA_HARNESS={harness!r}; expected 'claude_code' or 'miniswebench'")
    return normalized


@dataclass
class ValidationConfig:
    """All settings for a validation run, parsed once from spec + env.

    All defaults live in from_spec(). Field defaults here are only for
    type hints — from_spec always provides explicit values.
    """

    instructions: str = ""
    stories: list[dict] = field(default_factory=list)
    spec_settings: dict = field(default_factory=dict)
    validation_agent_harness: str = ""
    validation_agent_model: str = ""
    cc_model: str = ""
    cc_max_turns: int = 0
    cc_timeout_sec: int = 0
    script_timeout_sec: int = 0
    retry_timeout_sec: int = RETRY_TIMEOUT_SEC
    base_ref: str = ""
    agent_ref: str = ""

    @classmethod
    def from_spec(cls, spec: dict) -> ValidationConfig:
        """Build config from parsed TOML spec, overlaid with env vars."""
        settings = spec.get("settings", {})

        # CC model: spec setting > default. The default is a gateway-native slug
        # since gateway routing (VAL_AGENT_PORTKEY_KEY in verifier.env) is the
        # common case; for direct Anthropic, set cc_model to a plain name like
        # "claude-sonnet-4-6".
        cc_model = settings.get("cc_model") or "@bedrock/global.anthropic.claude-sonnet-4-6"
        # Default harness is mini-swe-agent (deterministic temp-0, fully
        # controlled prompt, version-pinned in test.sh). Set
        # FORCE_VA_HARNESS=claude_code to fall back to the Claude Code CLI.
        validation_agent_harness = _normalize_validation_agent_harness(
            os.environ.get("FORCE_VA_HARNESS") or "miniswebench"
        )
        validation_agent_model = os.environ.get("FORCE_VA_MODEL") or cc_model

        cc_max_turns = settings.get("cc_max_turns", 50)
        cc_timeout = settings.get("cc_timeout_sec", 900)
        script_timeout = settings.get("script_timeout_sec", 300)

        # Apply timeout multiplier if set (e.g., Modal runs are slower).
        # Harbor's --timeout-multiplier scales the verifier container timeout but
        # doesn't reach into validation script timeouts. Set TIMEOUT_MULTIPLIER
        # in verifier.env or the launch script to propagate.
        timeout_mult = float(os.environ.get("TIMEOUT_MULTIPLIER", "1"))
        retry_timeout = RETRY_TIMEOUT_SEC
        if timeout_mult > 1:
            script_timeout = int(script_timeout * timeout_mult)
            cc_timeout = int(cc_timeout * timeout_mult)
            retry_timeout = int(RETRY_TIMEOUT_SEC * timeout_mult)

        base_ref_path = Path("/var/lib/task_base_ref")
        base_ref = base_ref_path.read_text().strip() if base_ref_path.exists() else ""

        agent_ref_path = Path("/var/lib/agent_ref")
        agent_ref = agent_ref_path.read_text().strip() if agent_ref_path.exists() else ""

        # Fail fast if no usable credential is set (resolution lives in llm_utils).
        if not llm_utils.have_credentials():
            raise RuntimeError(
                "Validation requires PORTKEY_API_KEY, VAL_AGENT_PORTKEY_KEY, or ANTHROPIC_API_KEY to be set"
            )

        return cls(
            instructions=spec.get("instructions", ""),
            stories=spec.get("story", []),
            spec_settings=settings,
            validation_agent_harness=validation_agent_harness,
            validation_agent_model=validation_agent_model,
            cc_model=validation_agent_model,
            cc_max_turns=cc_max_turns,
            cc_timeout_sec=cc_timeout,
            script_timeout_sec=script_timeout,
            retry_timeout_sec=retry_timeout,
            base_ref=base_ref,
            agent_ref=agent_ref,
        )


def load_spec() -> dict | None:
    """Load and parse the validation spec TOML file. Returns None if absent."""
    if not VALIDATION_SPEC.exists():
        return None
    return tomllib.loads(VALIDATION_SPEC.read_text())


# ---------------------------------------------------------------------------
# Shared validation agent persona (used by both initial and retry prompts)
# ---------------------------------------------------------------------------

# Static prefix — the non-negotiable rules that every CC invocation must follow.
# Sourced from the shared library's mandate file (the SAME file the review judge
# scores against) so the initial prompt, the retry prompt, AND the judge all
# reference identical text. A single source prevents the agent and the judge
# from drifting apart (the retry agent was a collusion vector when it lacked the
# repo-immutability directive, and a judge that doesn't know the mandate can
# push the agent against it). ssb_lib is at tests/ssb_lib (/tests on sys.path).
from ssb_lib import llm_utils  # noqa: E402 — ssb_lib at tests/ssb_lib (/tests on sys.path)
from ssb_lib.validation_judge import DEFAULT_MANDATE_PATH  # noqa: E402

_AGENT_FUNDAMENTALS = DEFAULT_MANDATE_PATH.read_text().rstrip("\n")


# ---------------------------------------------------------------------------
# LLM Review (shared client setup)
# ---------------------------------------------------------------------------


def _get_review_client(config: ValidationConfig):
    """Build the Anthropic client + model for the LLM review judge.

    Delegates credential resolution, client construction (SDK max_retries=3),
    and model defaulting to the shared ``llm_utils`` module — the same plumbing
    the rubric/taste judges use. The review judge is a fixed reviewer, so the
    model is pinned (``env_var=None``: no ``MODEL_NAME`` override) and sourced
    from the shared constant. ``config`` already guaranteed credentials exist.
    """
    client = llm_utils.make_client(max_retries=3)
    if client is None:
        raise RuntimeError("validation review client requires gateway or Anthropic credentials")
    return client, llm_utils.judge_model(env_var=None)


SCRIPT_EXTENSIONS = (".py", ".tsx", ".ts", ".go", ".exs", ".rs")


def _is_validation_script(f: Path) -> bool:
    """True if *f* is a generated validation script (not a helper/fixture)."""
    return f.suffix in SCRIPT_EXTENSIONS and f.name != "conftest.py" and not f.name.startswith("validationParams")


def _collect_scripts() -> dict[str, str]:
    """Read generated validation scripts from SCRIPTS_DIR."""
    scripts: dict[str, str] = {}
    if not SCRIPTS_DIR.exists():
        return scripts
    for f in sorted(SCRIPTS_DIR.iterdir()):
        if _is_validation_script(f):
            scripts[f.name] = f.read_text()[:3000]
    return scripts


def _build_story_specs(config: ValidationConfig) -> str:
    """Build story specification text for review prompts."""
    parts: list[str] = []
    for story in config.stories:
        cases = story.get("test_case", [])
        if isinstance(cases, dict):
            cases = [cases]
        part = (
            f"### Story: {story['id']}\n"
            f"Driver: {story['driver']}\n"
            f"Description: {story.get('description', '')}\n"
            f"Procedure:\n{story.get('procedure', '')}\n"
        )
        if cases:
            part += f"First test case expected: {json.dumps(cases[0].get('expected', {}), indent=2)[:500]}\n"
        parts.append(part)
    return "\n".join(parts)


def _load_diagnostics() -> list[dict]:
    """Load parsed smoke-failure diagnostics from LOGS_DIR/diagnostics/."""
    diags: list[dict] = []
    diag_dir = LOGS_DIR / "diagnostics"
    if not diag_dir.exists():
        return diags
    for diag_file in sorted(diag_dir.glob("*_smoke_failure.json")):
        try:
            diags.append(json.loads(diag_file.read_text()))
        except (json.JSONDecodeError, OSError):
            continue
    return diags


# ---------------------------------------------------------------------------
# Stage 1: Script Generation (Claude Code)
# ---------------------------------------------------------------------------


def _get_patch_stat() -> str | None:
    """Run ``git apply --stat`` on agent.patch, return summary or empty string."""
    if not AGENT_PATCH.exists():
        return None
    try:
        r = subprocess.run(
            ["git", "apply", "--stat", str(AGENT_PATCH)],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=str(REPO_DIR),
            check=False,
        )
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except Exception as e:
        print(f"  git apply --stat failed (non-fatal): {e}", file=sys.stderr)
    return f"Failed to get patch stat, read the full diff at {AGENT_PATCH} instead."


def build_prompt(config: ValidationConfig) -> str:
    """Build the CC prompt from Jinja2 template."""
    import jinja2  # lazy: not needed when validation is skipped

    patch_stat = _get_patch_stat()

    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(PROMPT_TEMPLATE.parent)),
        keep_trailing_newline=True,
    )
    template = env.get_template(PROMPT_TEMPLATE.name)
    return template.render(
        agent_fundamentals=_AGENT_FUNDAMENTALS.format(scripts_dir=SCRIPTS_DIR, patch_path=str(AGENT_PATCH)),
        patch_stat=patch_stat,
        patch_path=str(AGENT_PATCH),
        instructions=config.instructions,
        stories=config.stories,
        scripts_dir=str(SCRIPTS_DIR),
    )


# ---------------------------------------------------------------------------
# CC Invocation
# ---------------------------------------------------------------------------


def _build_cc_args(model: str, max_turns: int) -> list[str]:
    """Build Claude CLI arguments (shared between root and non-root).

    Always uses stream-json output so each trial captures a full
    turn-by-turn trace in cc_stream.jsonl for debugging.
    """
    return [
        "--dangerously-skip-permissions",
        "--model",
        model,
        "--max-turns",
        str(max_turns),
        "--allowedTools",
        "Read,Write,Bash,Grep,Glob",
        "--output-format",
        "stream-json",
        "--verbose",
    ]


def _summarize_cc_stream(stream: str) -> str:
    """Reconstruct cc_output.txt's text summary from a stream-json capture.

    Event schema: https://code.claude.com/docs/en/headless#stream-responses
    """
    assistant_texts: list[str] = []
    result_line: str | None = None
    for raw_line in stream.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        try:
            evt = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if evt.get("type") == "assistant":
            for block in evt.get("message", {}).get("content", []):
                if block.get("type") == "text":
                    text = block.get("text", "")
                    if text.strip():
                        assistant_texts.append(text)
        elif evt.get("type") == "result":
            result_line = evt.get("result") or evt.get("subtype") or ""
    out = "\n".join(assistant_texts[-1:])
    if result_line:
        out = f"{result_line}\n\n{out}" if out else result_line
    return out


def _extract_cc_result_subtype(stream: str) -> str | None:
    """Extract the structured ``subtype`` of the final ``result`` event.

    The stream-json result event reports outcome via a machine-readable
    ``subtype`` ("success", "error_max_turns", "error_during_execution",
    API-error variants, …) plus an ``is_error`` flag — NOT via reconstructed
    prose like "Error: Reached max turns". This is the authoritative signal
    _detect_cc_failure keys off. Returns the last result event's subtype, or
    None if no result event was emitted (e.g. CC crashed before finishing).
    """
    subtype: str | None = None
    for raw_line in stream.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        try:
            evt = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if evt.get("type") == "result":
            sub = evt.get("subtype")
            if sub:
                subtype = str(sub)
            elif evt.get("is_error"):
                # Result event flagged an error but omitted a subtype: record a
                # generic marker so detection still treats it as a failure.
                subtype = "error"
    return subtype


def _build_cc_env(config: ValidationConfig) -> dict[str, str]:
    """Build the environment for the Claude Code subprocess."""
    env = {**os.environ}
    env.setdefault("PYTHONWARNINGS", "ignore::DeprecationWarning")
    env.update(llm_utils.agent_routing("claude_code", config.cc_model).env)
    return env


def _run_cc_subprocess(cmd: list[str], timeout: int, env: dict[str, str]) -> None:
    """Run CC subprocess, capture output, handle errors."""
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(REPO_DIR),
            check=False,
            env=env,
        )
        if result.stdout:
            # Per-invocation JSONL trace: each CC call (Stage 1, retry,
            # any future re-generations) gets its own numbered file so
            # retries don't clobber earlier traces.
            stream_dir = LOGS_DIR / "cc_stream"
            stream_dir.mkdir(parents=True, exist_ok=True)
            idx = len(list(stream_dir.glob("*.jsonl"))) + 1
            (stream_dir / f"{idx:02d}.jsonl").write_text(result.stdout)
            # cc_output.txt stays a single file overwritten per call — it holds
            # the human-readable summary for diagnostics.
            (LOGS_DIR / "cc_output.txt").write_text(_summarize_cc_stream(result.stdout)[:50000])
            # cc_result_subtype.txt persists the structured result subtype
            # (success / error_max_turns / error_during_execution / …) that
            # _detect_cc_failure keys off. Overwritten per call; removed when no
            # result event was emitted so a stale subtype can't mask a later
            # crash that produced no result.
            subtype_path = LOGS_DIR / "cc_result_subtype.txt"
            subtype = _extract_cc_result_subtype(result.stdout)
            if subtype:
                subtype_path.write_text(subtype)
            else:
                subtype_path.unlink(missing_ok=True)
        if result.returncode != 0 and result.stderr:
            print(f"  CC stderr: {result.stderr[:300]}", file=sys.stderr)
    except subprocess.TimeoutExpired:
        print("  CC timed out", file=sys.stderr)
    except FileNotFoundError:
        print("  'claude' CLI not found", file=sys.stderr)


def _invoke_cc_as_root(prompt: str, config: ValidationConfig, max_turns: int, timeout: int) -> None:
    """Invoke CC as non-root ccuser via su. Required when container runs as root."""
    subprocess.run(
        ["useradd", "-m", "-s", "/bin/bash", "ccuser"],
        capture_output=True,
        check=False,
    )
    for d in [REPO_DIR, SCRIPTS_DIR, LOGS_DIR]:
        subprocess.run(
            ["chmod", "-R", "a+rwX", str(d)],
            capture_output=True,
            check=False,
        )

    # Write prompt to file (can't pass via stdin through su)
    prompt_file = LOGS_DIR / "cc_prompt.txt"
    prompt_file.write_text(prompt)
    prompt_file.chmod(0o644)

    # Build env file — pass through all env vars for ccuser
    env = _build_cc_env(config)
    env_lines = ["export HOME='/home/ccuser'"]
    skip_keys = {"HOME"}
    for k, v in env.items():
        if k not in skip_keys:
            escaped = v.replace("'", "'\\''")
            env_lines.append(f"export {k}='{escaped}'")

    env_file = LOGS_DIR / "cc_env.sh"
    env_file.write_text("\n".join(env_lines) + "\n")
    env_file.chmod(0o644)

    cc_args = " ".join(shlex.quote(arg) for arg in _build_cc_args(config.cc_model, max_turns))
    wrapper = LOGS_DIR / "cc_wrapper.sh"
    wrapper.write_text(f"#!/bin/bash\nset -e\nsource '{env_file}'\ncat '{prompt_file}' | claude {cc_args} -p -\n")
    wrapper.chmod(0o755)

    cmd = ["su", "-s", "/bin/bash", "ccuser", "-c", str(wrapper)]
    _run_cc_subprocess(cmd, timeout, env)


def _invoke_cc_direct(prompt: str, config: ValidationConfig, max_turns: int, timeout: int) -> None:
    """Invoke CC directly (non-root case)."""
    cmd = ["claude", *_build_cc_args(config.cc_model, max_turns), "-p", prompt]
    _run_cc_subprocess(cmd, timeout, _build_cc_env(config))


def invoke_cc(prompt: str, config: ValidationConfig, max_turns: int, timeout: int) -> None:
    """Invoke CC, dispatching to root or direct mode."""
    routing = llm_utils.agent_routing("claude_code", config.cc_model)
    print(f"  CC routing: {routing.label} (model={routing.model})", file=sys.stderr)

    if os.getuid() == 0:
        _invoke_cc_as_root(prompt, config, max_turns, timeout)
    else:
        _invoke_cc_direct(prompt, config, max_turns, timeout)


def _write_validation_agent_status(  # noqa: PLR0913
    *,
    harness: str,
    model: str,
    returncode: int | None,
    elapsed_ms: int,
    invocation: int | None = None,
    exit_status: str = "",
    trajectory_path: Path | None = None,
    stdout_path: Path | None = None,
    stderr_path: Path | None = None,
    error: str = "",
) -> None:
    """Write last-invocation status for non-CC validation agents."""
    status = {
        "harness": harness,
        "model": model,
        "returncode": returncode,
        "elapsed_ms": elapsed_ms,
        "exit_status": exit_status,
        "invocation": invocation,
        "trajectory_path": str(trajectory_path) if trajectory_path else "",
        "stdout_path": str(stdout_path) if stdout_path else "",
        "stderr_path": str(stderr_path) if stderr_path else "",
        "error": error,
    }
    (LOGS_DIR / "validation_agent_status.json").write_text(json.dumps(status, indent=2))
    with (LOGS_DIR / "validation_agent_invocations.jsonl").open("a") as f:
        f.write(json.dumps(status) + "\n")


def _miniswebench_step_limit(max_turns: int) -> int:
    """Scale CC-style max turns to mini-swe-agent's finer-grained model calls."""
    if max_turns <= 0:
        return 0
    return math.ceil(max_turns * MINISWEBENCH_STEP_LIMIT_MULTIPLIER)


def _build_miniswe_system_prompt() -> str:
    """Build the harness-only mini-swe-agent system prompt."""
    return f"""\
You are a test engineer generating validation scripts for Staff-Bench.

You operate ONLY through bash tool calls — there is no file editor available.
This is an interactive loop: you think, issue at least one bash command, read
the result, then continue.

For each response:
- Include brief reasoning text explaining what you are doing.
- Include at least one bash tool call. A response with no tool call is wasted.
- Use multiple bash tool calls in one response when the commands are independent.
- Directory and environment changes are NOT persistent across commands. Use
  absolute paths or combine dependent operations into a single command.

## Workflow

1. Read the developer's diff first, then the harness files in /tests/validate/,
   then only the exact source files you need for imports and signatures. Do not
   list directories broadly or read unrelated code.
2. Write each required validation script into {SCRIPTS_DIR}.
3. Before finishing, re-read each script you wrote (cat it) and check it against
   the story procedure — correct imports, real code paths, every expected key
   asserted.

## Writing scripts (bash-only)

With no file editor, create each script with a heredoc redirect into
{SCRIPTS_DIR}. Quote the delimiter ('SCRIPT') so the shell does not expand or
mangle anything in the test body:

cat > {SCRIPTS_DIR}/<story_id>.py <<'SCRIPT'
# ...full script contents...
SCRIPT

You MUST write your scripts into {SCRIPTS_DIR}. You MUST NOT create, modify, or
delete any file under /repo/ or /tests/ — the prohibition on editing those trees
does NOT apply to {SCRIPTS_DIR}, which is the one place your scripts belong.

Exercise the submitted implementation honestly. Do not run the test suites
yourself (go test, pytest, jest, mix test, cargo test) — the framework
smoke-tests your scripts automatically after you finish. If a function, class,
or endpoint you need does not exist, let the test fail; never implement it.

## Finishing

When every required validation script has been written and reviewed, finish by
running exactly this command and nothing else:

echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT

Do not combine that completion command with any other shell command. Always end
the session this way — a run that stops without it is treated as incomplete.
"""


def _build_miniswe_config(
    *,
    system_prompt_path: Path,
    user_prompt_path: Path,
    output_path: Path,
    max_turns: int,
    model: str,
    model_class: str,
    provider: str,
) -> str:
    """Write a debug config snapshot for mini-swe-agent runs."""
    return json.dumps(
        {
            "agent": {
                "agent_class": "default",
                "system_prompt_path": str(system_prompt_path),
                "instance_template": "{{ task }}",
                "step_limit": max_turns,
                "cost_limit": 0,
                "output_path": str(output_path),
            },
            "environment": {
                "environment_class": "local",
                "cwd": str(REPO_DIR),
                "timeout": 600,
            },
            "model": {
                "model_name": model,
                "model_class": model_class,
                "provider": provider,
                "model_kwargs": {
                    "drop_params": True,
                    "temperature": 0.0,
                    "parallel_tool_calls": True,
                },
                "cost_tracking": "ignore_errors",
                "observation_template": "mini_truncated_json",
            },
            "run": {
                "user_prompt_path": str(user_prompt_path),
            },
        },
        indent=2,
    )


def _write_miniswe_runner(runner_path: Path) -> None:
    """Write the small Python entrypoint used to invoke mini-swe-agent."""
    runner_path.write_text(
        """#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from minisweagent.agents.default import DefaultAgent
from minisweagent.environments.local import LocalEnvironment
from minisweagent.models import get_model


OBSERVATION_TEMPLATE = \"\"\"\
{%- if output.output | length < 10000 -%}
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
\"\"\"


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
            "Tool call error:\\n<error>{{ error }}</error>\\n"
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
"""
    )
    runner_path.chmod(0o755)


def _parse_miniswe_exit_status(stdout: str, trajectory_path: Path) -> str:
    """Extract mini-swe-agent's exit status from stdout or trajectory."""
    text = (stdout or "").strip()
    if text:
        try:
            obj = json.loads(text)
            status = obj.get("exit_status")
            if isinstance(status, str):
                return status
        except json.JSONDecodeError:
            pass
    if trajectory_path.exists():
        try:
            obj = json.loads(trajectory_path.read_text())
            status = obj.get("info", {}).get("exit_status")
            if isinstance(status, str):
                return status
        except (json.JSONDecodeError, OSError):
            pass
    return ""


def _build_miniswe_env(config: ValidationConfig) -> dict[str, str]:
    """Build the environment for the mini-swe-agent subprocess."""
    env = {**os.environ}
    env.setdefault("PYTHONWARNINGS", "ignore::DeprecationWarning")
    env["MSWEA_CONFIGURED"] = "true"
    env["MSWEA_SILENT_STARTUP"] = "1"
    env["MSWEA_GLOBAL_CONFIG_DIR"] = str(LOGS_DIR / "miniswebench_config")
    env.setdefault("PIP_PROGRESS_BAR", "off")
    env.setdefault("TQDM_DISABLE", "1")
    env.update(llm_utils.agent_routing("miniswebench", config.validation_agent_model).env)
    return env


def _invoke_miniswebench(prompt: str, config: ValidationConfig, max_turns: int, timeout: int) -> None:
    """Invoke mini-swe-agent as the validation script generator."""
    mini_dir = LOGS_DIR / "miniswebench"
    mini_dir.mkdir(parents=True, exist_ok=True)
    step_limit = _miniswebench_step_limit(max_turns)

    idx = len(list(mini_dir.glob("*.stdout.txt"))) + 1
    system_prompt_path = mini_dir / f"{idx:02d}.system_prompt.md"
    user_prompt_path = mini_dir / f"{idx:02d}.user_prompt.md"
    output_path = mini_dir / f"{idx:02d}.trajectory.json"
    runner_path = mini_dir / "run_validation_agent.py"
    system_prompt_path.write_text(_build_miniswe_system_prompt())
    user_prompt_path.write_text(prompt)
    _write_miniswe_runner(runner_path)

    routing = llm_utils.agent_routing("miniswebench", config.validation_agent_model)

    config_path = mini_dir / "validation_config.yaml"
    config_path.write_text(
        _build_miniswe_config(
            system_prompt_path=system_prompt_path,
            user_prompt_path=user_prompt_path,
            output_path=output_path,
            max_turns=step_limit,
            model=routing.model,
            model_class=routing.model_class,
            provider=routing.provider,
        )
    )

    cmd = [
        sys.executable,
        str(runner_path),
        "--system-prompt",
        str(system_prompt_path),
        "--user-prompt",
        str(user_prompt_path),
        "--output",
        str(output_path),
        "--model",
        routing.model,
        "--cwd",
        str(REPO_DIR),
        "--command-timeout",
        "600",
    ]
    if routing.model_class:
        cmd.extend(["--model-class", routing.model_class])
    if routing.provider:
        cmd.extend(["--provider", routing.provider])
    if step_limit > 0:
        cmd.extend(["--step-limit", str(step_limit)])

    start = time.monotonic()
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(REPO_DIR),
            check=False,
            env=_build_miniswe_env(config),
        )
        elapsed_ms = int((time.monotonic() - start) * 1000)
        stdout_path = mini_dir / f"{idx:02d}.stdout.txt"
        stderr_path = mini_dir / f"{idx:02d}.stderr.txt"
        stdout_path.write_text(result.stdout or "")
        stderr_path.write_text(result.stderr or "")
        summary = (result.stdout or result.stderr or "").strip()
        (LOGS_DIR / "va_output.txt").write_text(summary[:50000])
        exit_status = _parse_miniswe_exit_status(result.stdout or "", output_path)
        if result.returncode != 0 and result.stderr:
            print(f"  miniswebench stderr: {result.stderr[:300]}", file=sys.stderr)
        _write_validation_agent_status(
            harness="miniswebench",
            model=config.validation_agent_model,
            returncode=result.returncode,
            elapsed_ms=elapsed_ms,
            invocation=idx,
            exit_status=exit_status,
            trajectory_path=output_path,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
        )
    except subprocess.TimeoutExpired as e:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        error = f"miniswebench timed out after {timeout}s"
        (LOGS_DIR / "va_output.txt").write_text(error)
        stdout_path = mini_dir / "timeout.stdout.txt"
        stderr_path = mini_dir / "timeout.stderr.txt"
        stdout_path.write_text(e.stdout.decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or ""))
        stderr_path.write_text(e.stderr.decode(errors="replace") if isinstance(e.stderr, bytes) else (e.stderr or ""))
        print(f"  {error}", file=sys.stderr)
        _write_validation_agent_status(
            harness="miniswebench",
            model=config.validation_agent_model,
            returncode=None,
            elapsed_ms=elapsed_ms,
            invocation=idx,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            error=error,
        )
    except FileNotFoundError:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        error = "mini-swe-agent is not installed"
        (LOGS_DIR / "va_output.txt").write_text(error)
        print(f"  {error}", file=sys.stderr)
        _write_validation_agent_status(
            harness="miniswebench",
            model=config.validation_agent_model,
            returncode=None,
            elapsed_ms=elapsed_ms,
            invocation=idx,
            error=error,
        )


def invoke_validation_agent(prompt: str, config: ValidationConfig, max_turns: int, timeout: int) -> None:
    """Invoke the selected validation script generation harness."""
    if config.validation_agent_harness == "claude_code":
        invoke_cc(prompt, config, max_turns, timeout)
    elif config.validation_agent_harness == "miniswebench":
        _invoke_miniswebench(prompt, config, max_turns, timeout)
    else:
        raise RuntimeError(f"Unsupported validation agent harness: {config.validation_agent_harness}")


# ---------------------------------------------------------------------------
# Stage 1.5: Smoke Test + Self-Healing
# ---------------------------------------------------------------------------


def smoke_test_stories(config: ValidationConfig) -> tuple[bool, str]:
    """Run the first test case of each story as a smoke test.

    Returns (passed, error_message). Also writes diagnostics for failed stories.
    """
    if not config.stories:
        return True, ""

    errors: list[str] = []
    for story in config.stories:
        cases = story.get("test_case", [])
        if isinstance(cases, dict):
            cases = [cases]
        if not cases:
            continue

        smoke_story = {**story, "test_case": cases[0]}
        result = execute_single_story(smoke_story, config)

        if not result.all_passed:
            sid = story["id"]
            failed_cases = [c for c in result.cases if not c.passed]
            reason = failed_cases[0].reason if failed_cases else "unknown"
            errors.append(f"[{sid}] {reason}")

            # Write diagnostic details for the retry agent and human review
            diag_dir = LOGS_DIR / "diagnostics"
            diag_dir.mkdir(parents=True, exist_ok=True)
            script_path = SCRIPTS_DIR / f"{sid}.py"
            if not script_path.exists():
                for ext in [".test.tsx", ".test.ts", "_test.go", "_test.rs"]:
                    candidate = SCRIPTS_DIR / f"{sid}{ext}"
                    if candidate.exists():
                        script_path = candidate
                        break
            script_content = script_path.read_text() if script_path.exists() else "NOT GENERATED"
            diag = {
                "story_id": sid,
                "smoke_error": reason,
                "script_content": script_content[:3000],
                "stderr": failed_cases[0].stderr[:2000] if failed_cases and failed_cases[0].stderr else "",
                "stdout": failed_cases[0].stdout[:2000] if failed_cases and failed_cases[0].stdout else "",
            }
            (diag_dir / f"{sid}_smoke_failure.json").write_text(json.dumps(diag, indent=2))

    if errors:
        return False, "; ".join(errors)
    return True, ""


def _build_judge_inputs(
    config: ValidationConfig,
    *,
    smoke_results: dict | None = None,
    execution_results: dict | None = None,
):
    """Construct ``JudgeInputs`` from the in-container artefacts.

    ``ssb_lib.validation_judge`` is imported lazily here so the heavy judge
    deps (jinja2/pydantic) load only when validation actually runs.
    """
    from ssb_lib.validation_judge import JudgeInputs  # noqa: PLC0415 — lazy

    spec_text = VALIDATION_SPEC.read_text() if VALIDATION_SPEC.exists() else ""
    agent_patch_text = AGENT_PATCH.read_text() if AGENT_PATCH.exists() else ""
    diag = _load_diagnostics()
    diagnostics: dict | None = None
    if diag:
        diagnostics = {"per_story": [d for d in diag]}

    return JudgeInputs(
        validation_spec_text=spec_text,
        spec_instructions=config.instructions,
        stories=config.stories,
        generated_scripts=_collect_scripts(),
        agent_patch=agent_patch_text,
        smoke_results=smoke_results,
        execution_results=execution_results,
        execution_diagnostics=diagnostics,
    )


def _summarize_smoke(passed: bool, error: str) -> dict:
    """Build a small JSON-serializable summary the unified judge prompt
    formats into the smoke section of its prompt."""
    return {
        "smoke_passed": passed,
        "first_failure_summary": (error or "")[:1000],
    }


def run_pre_retry_judge(config: ValidationConfig, smoke_passed: bool, smoke_error: str):
    """Stage 1.6: Pre-retry unified-judge call.

    Replaces the legacy ``review_validation_scripts`` LLM-review-and-retry
    trigger. Returns a ``JudgeResult`` whose ``severity.discard_recommended``
    indicates whether the trial should be killed before retry, and whose
    ``actionable`` carries imperative engineering advice for retry.

    Returns ``None`` if no scripts were generated (CC infra failure case)
    so the caller can short-circuit without an LLM call.
    """
    from ssb_lib.validation_judge import judge as _unified_judge  # noqa: PLC0415

    if not _collect_scripts():
        return None

    inputs = _build_judge_inputs(
        config,
        smoke_results=_summarize_smoke(smoke_passed, smoke_error),
    )
    client, model = _get_review_client(config)
    result = _unified_judge(client, inputs, model=model)

    (LOGS_DIR / "validation_review_pre.json").write_text(json.dumps(result.severity.dump_dict(), indent=2))
    flags = []
    if result.severity.discard_recommended:
        flags.append(f"DISCARD ({','.join(result.severity.discard_reasons)})")
    if result.actionable:
        flags.append(f"{len(result.actionable)} actionable")
    status = ", ".join(flags) if flags else "CLEAN"
    print(f"  Stage 1.6: Pre-retry judge — {status}", file=sys.stderr)
    return result


def regenerate_with_feedback(
    config: ValidationConfig,
    error_msg: str,
    actionable: list | None = None,
) -> None:
    """Stage 1.7: Invoke the validation agent again with self-healing feedback.

    Carries forward what was useful in the legacy retry prompt (story summary,
    raw smoke diagnostics, the (a)-vs-(b) diagnose framing) and replaces the
    "An independent reviewer identified these issues" block with the unified
    judge's ``actionable`` suggestions. The judge's gating-grade ``severity``
    findings are NEVER routed here — only the ``actionable`` channel, which
    the judge prompt constrains to imperative engineering advice with no
    review-process vocabulary, reaches the retrying agent.

    ``actionable`` is a list of ``ActionableSuggestion`` instances (or empty).
    """
    # Build a brief context summary so the agent knows what it's fixing
    story_summary = "\n".join(
        f"- {s['id']} (driver: {s['driver']}): {s.get('description', '').strip()[:80]}" for s in config.stories
    )

    # Include diagnostic details if available — preserved verbatim from today.
    # The agent needs raw smoke errors and stderr to fix bugs; judge
    # interpretations alone are not enough.
    diag_details = ""
    for diag in _load_diagnostics():
        diag_details += (
            f"\n### {diag['story_id']}\n"
            f"Error: {diag['smoke_error'][:300]}\n"
            f"Script ({SCRIPTS_DIR / diag['story_id']}*):\n"
            f"```\n{diag['script_content'][:1500]}\n```\n"
        )
        if diag.get("stderr"):
            diag_details += f"stderr:\n```\n{diag['stderr'][:500]}\n```\n"

    fundamentals = _AGENT_FUNDAMENTALS.format(scripts_dir=SCRIPTS_DIR, patch_path=str(AGENT_PATCH))
    fix_prompt = (
        f"{fundamentals}\n\n"
        f"Test harness: `/tests/validate/test_harness.py` — read it for available utilities.\n"
        f"Scripts dir: {SCRIPTS_DIR}\n"
        f"Repo: {REPO_DIR}\n\n"
        f"## Task: Fix validation test scripts\n\n"
        f"Stories being tested:\n{story_summary}\n\n"
    )
    if error_msg:
        fix_prompt += f"Some scripts failed during smoke testing:\n\n```\n{error_msg[:800]}\n```\n"
    if diag_details:
        fix_prompt += f"\n## Failure Details\n{diag_details}\n"
    if actionable:
        bullets = []
        for entry in actionable[:10]:
            sid_label = f"[{entry.story_id}] " if getattr(entry, "story_id", None) else ""
            bullets.append(f"- {sid_label}{entry.suggestion}")
        fix_prompt += (
            "\n## Engineering suggestions\n\n"
            "Concrete improvements identified in the current scripts:\n\n" + "\n".join(bullets) + "\n"
        )
    fix_prompt += (
        f"\nDiagnose whether each failure is:\n"
        f"(a) A bug in the TEST SCRIPT (wrong import, typo, incorrect API usage)\n"
        f"(b) A legitimate failure because the implementation doesn't do what the "
        f"story describes\n\n"
        f"For (a): Fix the TEST SCRIPT in {SCRIPTS_DIR}. Read source in {REPO_DIR} "
        f"to understand the correct API, but do NOT modify any repo files.\n"
        f"For (b): Leave the test as-is — a failing test is correct if the "
        f"implementation doesn't match the expected behavior."
    )
    print("  Stage 1.7: Retrying with error feedback...", file=sys.stderr)
    invoke_validation_agent(fix_prompt, config, max_turns=RETRY_MAX_TURNS, timeout=config.retry_timeout_sec)


# ---------------------------------------------------------------------------
# Repo Integrity (anti-collusion)
# ---------------------------------------------------------------------------


def _git(args: list[str], **kwargs) -> subprocess.CompletedProcess:
    """Run a git command inside REPO_DIR with safe defaults."""
    return subprocess.run(
        ["git"] + args,
        cwd=str(REPO_DIR),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        **kwargs,
    )


def create_pre_cc_checkpoint(config: ValidationConfig) -> str | None:
    """Commit the current repo state (including oracle changes) as a checkpoint.

    Returns the checkpoint ref, or None if checkpointing is not possible.
    """
    if not config.base_ref:
        return None
    try:
        _git(["add", "-A"])
        _git(
            [
                "-c",
                "user.email=val@staff-bench",
                "-c",
                "user.name=validation",
                "commit",
                "--allow-empty",
                "-m",
                "pre-cc-checkpoint",
                "--no-gpg-sign",
            ]
        )
        ref = _git(["rev-parse", "HEAD"]).stdout.strip()
        print(f"  Pre-CC checkpoint: {ref[:12]}", file=sys.stderr)
        return ref
    except Exception as e:
        print(f"  Pre-CC checkpoint error (non-fatal): {e}", file=sys.stderr)
        return None


def run_validation_agent_pipeline(config: ValidationConfig) -> tuple[str | None, str | None]:
    """Run script generation (Stage 1), smoke test (1.5), unified pre-retry
    judge (1.6), and retry (1.7).

    Returns ``(infra_failure_reason, judge_discard_reason)``. Either may be
    ``None``. ``judge_discard_reason`` is set when the pre-retry judge gave
    a gating ``score == 0`` finding — the trial should be killed without
    running execution or retrying. Caller is responsible for the actual
    sys.exit; this function never exits.
    """
    print(
        f"  Stage 1: Generating scripts via {config.validation_agent_harness} "
        f"(model={config.validation_agent_model})...",
        file=sys.stderr,
    )
    prompt = build_prompt(config)
    invoke_validation_agent(prompt, config, config.cc_max_turns, config.cc_timeout_sec)

    failure_reason = _detect_validation_agent_failure(config)
    if failure_reason:
        print(f"  Validation agent infrastructure failure: {failure_reason}", file=sys.stderr)

    passed, error = smoke_test_stories(config)
    if not passed:
        print(f"  Stage 1.5: Smoke test failed: {error[:200]}", file=sys.stderr)

    # Stage 1.6: pre-retry unified judge — discard on gating == 0 signal.
    judge_result = run_pre_retry_judge(config, passed, error)
    if judge_result is not None and judge_result.severity.discard_recommended:
        return failure_reason, ",".join(judge_result.severity.discard_reasons)

    actionable = list(judge_result.actionable) if judge_result else []

    retried = False
    if not passed or actionable:
        retried = True
        regenerate_with_feedback(config, error, actionable=actionable)

    failure_reason = _detect_validation_agent_failure(config)
    if retried and failure_reason:
        print(f"  Validation agent infrastructure failure after retry: {failure_reason}", file=sys.stderr)

    return failure_reason, None


def check_repo_integrity(pre_cc_ref: str | None, config: ValidationConfig) -> str | None:
    """Detect if CC modified repo files (collusion).

    Compares the current repo state against pre_cc_ref (which includes oracle
    changes).  Catches both committed changes (CC ran git commit) and
    uncommitted working-tree changes.  If a violation is found, resets the repo
    to the pre-CC state so validation runs on a clean codebase.

    Returns the violation detail string, or None if clean.
    """
    check_ref = pre_cc_ref or config.base_ref
    if not check_ref:
        # No baseline to diff against — we CANNOT verify the agent left /repo
        # untouched. Fail CLOSED: raise so the trial is invalidated (exit 2) by
        # the top-level handler, rather than silently treated as clean (a pass).
        raise RuntimeError("integrity check: no base ref available — cannot verify repo integrity")
    try:
        # Three kinds of CC modification to detect:
        # 1. CC made new commits (moved HEAD past the checkpoint)
        committed = _git(["diff", "--name-only", check_ref, "HEAD"]).stdout.strip()
        # 2. CC modified tracked files in the working tree
        modified = _git(["diff", "--name-only", check_ref]).stdout.strip()
        # 3. CC created new (untracked) files — git diff misses these
        untracked = _git(["ls-files", "--others", "--exclude-standard"]).stdout.strip()

        # Collect all changed paths, excluding __validation__/ directories.
        # CC writes jest test files into __validation__/ inside the repo because
        # Jest needs them in the source tree for import resolution.  The jest
        # driver copies them there during Stage 2 anyway — not collusion.
        all_paths = set()
        for block in (committed, modified, untracked):
            for line in block.splitlines():
                path = line.strip()
                if path and "/__validation__/" not in path and not path.startswith("__validation__/"):
                    all_paths.add(path)

        if not all_paths:
            return None

        cc_changes = "\n".join(sorted(all_paths))
        print(
            f"  INTEGRITY VIOLATION: CC modified repo files:\n  {cc_changes[:300]}",
            file=sys.stderr,
        )
        # Reset: undo CC commits, restore tracked files, remove untracked files
        _git(["reset", "--hard", check_ref])
        _git(["clean", "-fd"])
        print("  Repo reset to pre-CC state.", file=sys.stderr)
        return cc_changes
    except Exception as e:
        # Fail CLOSED: a git error means integrity is UNVERIFIED. Do not return
        # None (which reads as 'clean'/pass) — re-raise so the trial is
        # invalidated (exit 2) rather than passing unverified.
        raise RuntimeError(f"integrity check failed to run — repo integrity unverified: {e}") from e


# ---------------------------------------------------------------------------
# Stage 2: Execution
# ---------------------------------------------------------------------------

# Import drivers from the ssb_lib package (tests/ on sys.path; ssb_lib at tests/ssb_lib)
sys.path.insert(0, str(Path(__file__).parent))
from ssb_lib.drivers import get_driver  # noqa: E402
from ssb_lib.drivers.base import CaseResult, DriverResult  # noqa: E402


def execute_single_story(story: dict, config: ValidationConfig) -> DriverResult:
    """Execute all test cases for a single story using its driver."""
    sid = story["id"]
    driver_name = story["driver"]

    try:
        driver = get_driver(driver_name)
    except ValueError as e:
        result = DriverResult(story_id=sid)
        result.cases.append(CaseResult(case_index=0, passed=False, reason=str(e)))
        return result

    return driver.execute_story(
        story=story,
        scripts_dir=SCRIPTS_DIR,
        logs_dir=LOGS_DIR,
        repo_dir=REPO_DIR,
        timeout=config.script_timeout_sec,
        spec_settings=config.spec_settings,
    )


def execute_all_stories(config: ValidationConfig) -> tuple[list[dict], int, int]:
    """Stage 2: Execute all stories and collect results."""
    results: list[dict] = []
    total_cases = 0
    passed_cases = 0

    for story in config.stories:
        sid = story["id"]
        driver_result = execute_single_story(story, config)

        for case in driver_result.cases:
            total_cases += 1
            if case.passed:
                passed_cases += 1

        if driver_result.all_passed:
            print(f"  [{sid}] PASS ({driver_result.total_count} cases)", file=sys.stderr)
            results.append(
                {
                    "story_id": sid,
                    "pass": True,
                    "reason": "OK",
                    "cases": driver_result.total_count,
                }
            )
        else:
            reasons = [c.reason for c in driver_result.cases if not c.passed]
            reason_str = "; ".join(reasons)
            print(f"  [{sid}] FAIL: {reason_str[:120]}", file=sys.stderr)
            results.append(
                {
                    "story_id": sid,
                    "pass": False,
                    "reason": reason_str,
                    "cases": driver_result.total_count,
                }
            )

    return results, passed_cases, total_cases


# ---------------------------------------------------------------------------
# Stage 2.5: Post-fix review (collusion / cheating / misalignment)
# ---------------------------------------------------------------------------


def run_post_exec_judge(config: ValidationConfig):
    """Stage 2.5: Post-execution unified-judge call.

    Replaces the legacy ``post_fix_review``. Reads
    ``validation_results.json`` (already written by Stage 2 via
    ``write_results``), feeds it into the unified judge alongside the same
    spec/scripts/agent.patch context as the pre-retry judge, and returns
    the ``JudgeResult``. Caller decides whether to discard the trial.

    Side effects:
      - writes ``validation_review_final.json`` (severity-side, full
        rationales).
      - merges per-criterion scores + the discard flag into
        ``reward.json`` and ``validation_results.json`` (keys
        ``review_judge_overall``, ``review_judge_per_criterion``,
        ``review_judge_discard_recommended``, ``review_judge_discard_reasons``).
    """
    from ssb_lib.validation_judge import judge as _unified_judge  # noqa: PLC0415

    if not _collect_scripts():
        return None

    exec_results: dict | None = None
    results_path = LOGS_DIR / "validation_results.json"
    if results_path.exists():
        try:
            exec_results = json.loads(results_path.read_text())
        except Exception as e:
            print(
                f"  Stage 2.5: Failed to read validation_results.json: {e}",
                file=sys.stderr,
            )

    inputs = _build_judge_inputs(config, execution_results=exec_results)
    client, model = _get_review_client(config)
    result = _unified_judge(client, inputs, model=model)

    severity_dump = result.severity.dump_dict()
    (LOGS_DIR / "validation_review_final.json").write_text(json.dumps(severity_dump, indent=2))

    # Surface the new judge result on reward.json + validation_results.json
    # so downstream consumers (sbgen analyze, explorer) see the structured
    # scoring without reading a separate file. No backwards-compat boolean
    # projection — old explorer JSONs in src/explorer/data will be
    # regenerated on the next sbgen analyze pass.
    review_flags = {
        "review_judge_overall": result.severity.overall,
        "review_judge_per_criterion": result.severity.per_criterion,
        "review_judge_discard_recommended": result.severity.discard_recommended,
        "review_judge_discard_reasons": list(result.severity.discard_reasons),
    }
    for path in [LOGS_DIR / "validation_results.json", LOGS_DIR / "reward.json"]:
        if path.exists():
            data = json.loads(path.read_text())
            data.update(review_flags)
            path.write_text(json.dumps(data, indent=2))

    if result.severity.discard_recommended:
        status = f"DISCARD ({','.join(result.severity.discard_reasons)})"
    else:
        status = f"score={result.severity.overall:.2f}"
    print(f"  Stage 2.5: Post-exec judge — {status}", file=sys.stderr)
    return result


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


def _detect_cc_failure(cc_output: Path, config: ValidationConfig) -> str | None:
    """Check if CC had an infrastructure failure (distinct from test failures).

    Failure is determined from the structured result ``subtype`` persisted to
    ``cc_result_subtype.txt`` by _run_cc_subprocess — NOT from reconstructed
    prose. The stream-json path never emits prefixes like
    "Error: Reached max turns"; it emits a result event whose ``subtype`` is
    one of "success", "error_max_turns", "error_during_execution", or an
    API-error variant. Any non-"success" subtype is an infra failure.

    Returns a reason string if CC failed due to infra issues, None otherwise.
    """
    subtype_path = cc_output.parent / "cc_result_subtype.txt"
    subtype = subtype_path.read_text().strip() if subtype_path.exists() else ""

    if subtype:
        if subtype != "success":
            return _cc_subtype_reason(subtype)
    elif not cc_output.exists():
        # No result subtype AND no summary output — CC never produced anything;
        # likely crashed before emitting a result event or wasn't installed.
        return "CC produced no output (not installed or crashed)"

    # Check if any scripts were generated (count only, don't read contents).
    # Retained as a fallback: a "success" subtype with zero scripts still means
    # the validation agent did not do its job.
    script_count = sum(1 for f in SCRIPTS_DIR.iterdir() if _is_validation_script(f)) if SCRIPTS_DIR.exists() else 0
    expected = len(config.stories)
    if script_count == 0 and expected > 0:
        return f"CC generated 0/{expected} scripts"

    return None


def _cc_subtype_reason(subtype: str) -> str:
    """Map a non-success CC result subtype to a human-readable failure reason."""
    known = {
        "error_max_turns": "CC exhausted max turns",
        "error_during_execution": "CC errored during execution",
    }
    return f"{known.get(subtype, f'CC failed with subtype {subtype!r}')} (subtype={subtype})"


def _detect_miniswebench_failure(config: ValidationConfig) -> str | None:
    """Check if mini-swe-agent had an infrastructure failure."""
    status_path = LOGS_DIR / "validation_agent_status.json"
    status: dict = {}
    if status_path.exists():
        try:
            status = json.loads(status_path.read_text())
        except json.JSONDecodeError:
            status = {}

    output_path = LOGS_DIR / "va_output.txt"
    if not output_path.exists() and not status:
        return "miniswebench produced no output (not installed or crashed)"

    text = output_path.read_text().strip() if output_path.exists() else ""
    if "timed out" in text.lower() or status.get("error", "").lower().find("timed out") >= 0:
        return f"miniswebench timed out ({(text or status.get('error', ''))[:200]})"

    exit_status = status.get("exit_status")
    if exit_status and exit_status != "Submitted":
        return f"miniswebench exited with {exit_status}"
    if status.get("returncode") not in (None, 0):
        return f"miniswebench exited with returncode={status['returncode']}"

    script_count = sum(1 for f in SCRIPTS_DIR.iterdir() if _is_validation_script(f)) if SCRIPTS_DIR.exists() else 0
    expected = len(config.stories)
    if script_count == 0 and expected > 0:
        detail = ""
        if status.get("returncode") not in (None, 0):
            detail = f"; returncode={status['returncode']}"
        return f"miniswebench generated 0/{expected} scripts{detail}"

    return None


def _detect_validation_agent_failure(config: ValidationConfig) -> str | None:
    """Check if the selected validation agent had an infrastructure failure."""
    if config.validation_agent_harness == "claude_code":
        return _detect_cc_failure(LOGS_DIR / "cc_output.txt", config)
    if config.validation_agent_harness == "miniswebench":
        return _detect_miniswebench_failure(config)
    return f"unsupported validation agent harness: {config.validation_agent_harness}"


def write_results(
    results: list[dict],
    passed_cases: int,
    total_cases: int,
    cc_failure: str | None = None,
    integrity_violation: str | None = None,
    judge_discard: str | None = None,
    config: ValidationConfig | None = None,
) -> None:
    """Write validation results to JSON files.

    ``judge_discard`` is set when the unified judge gave a gating ``score == 0``
    finding at either Stage 1.6 (pre-retry) or Stage 2.5 (post-execution).
    Treated like ``integrity_violation`` for the purposes of file output:
    reward.json is not refreshed with story scores so harbor's
    ``RewardFileEmptyError`` path triggers downstream.
    """
    passed_stories = sum(1 for r in results if r["pass"])
    total_stories = len(results)
    score = round(passed_stories / total_stories, 3) if total_stories > 0 else 0.0

    failure_label = f" | VALIDATION AGENT FAILURE: {cc_failure}" if cc_failure else ""
    parts = f"Stories: {passed_stories}/{total_stories} | Cases: {passed_cases}/{total_cases}"
    summary = f"{parts} | Score: {score}{failure_label}"
    print(f"\n  {summary}", file=sys.stderr)

    out = {
        "validation_score": score,
        "passed_stories": passed_stories,
        "total_stories": total_stories,
        "passed_cases": passed_cases,
        "total_cases": total_cases,
        "stories": {r["story_id"]: {"pass": r["pass"], "reason": r["reason"]} for r in results},
    }
    if config:
        out["validation_agent_harness"] = config.validation_agent_harness
        out["validation_agent_model"] = config.validation_agent_model
    if cc_failure:
        out["validation_agent_infrastructure_failure"] = cc_failure
        if config and config.validation_agent_harness == "claude_code":
            out["cc_infrastructure_failure"] = cc_failure
    if integrity_violation:
        out["integrity_violation"] = integrity_violation
        out["integrity_violation_detail"] = integrity_violation[:500]
    if judge_discard:
        out["judge_discard"] = judge_discard
    (LOGS_DIR / "validation_results.json").write_text(json.dumps(out, indent=2))

    if not integrity_violation and not judge_discard:
        reward_path = LOGS_DIR / "reward.json"
        reward = json.loads(reward_path.read_text()) if reward_path.exists() else {}
        reward["validation_score"] = out["validation_score"]
        reward["validation_stories"] = out["stories"]
        if config:
            reward["validation_agent_harness"] = config.validation_agent_harness
            reward["validation_agent_model"] = config.validation_agent_model
        reward_path.write_text(json.dumps(reward, indent=2))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    print("=" * 60, file=sys.stderr)
    print("  VALIDATION: User Story Verification", file=sys.stderr)
    print("=" * 60, file=sys.stderr)

    spec = load_spec()
    if spec is None:
        print("  No validation_spec.toml found, skipping.", file=sys.stderr)
        return

    config = ValidationConfig.from_spec(spec)
    if not config.stories:
        return

    ref_label = config.base_ref[:12] if config.base_ref else "none"
    print(
        f"  Base ref: {ref_label} | Stories: {len(config.stories)} | "
        f"VA: {config.validation_agent_harness}/{config.validation_agent_model}",
        file=sys.stderr,
    )

    SCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    # --- Stage 0.5 → 1.75: validation-agent generation + pre-retry judge + integrity check ---
    pre_cc_ref = create_pre_cc_checkpoint(config)
    cc_failure_reason, judge_pre_discard = run_validation_agent_pipeline(config)

    if judge_pre_discard:
        _kill_trial(config, judge_discard=f"pre-retry: {judge_pre_discard}")

    integrity_violation = check_repo_integrity(pre_cc_ref, config)
    if integrity_violation:
        _kill_trial(config, integrity_violation=integrity_violation)

    # --- Stage 2: Execute and verify ---
    print("  Stage 2: Executing and verifying...", file=sys.stderr)
    results, passed_cases, total_cases = execute_all_stories(config)

    # A transient agent failure — CC hitting max turns / an API blip before a
    # successful retry, or mini-swe-agent exiting non-"Submitted" (step limit
    # reached, completion command skipped, late timeout) — is forgiven when
    # every executed case still passed. The scripts are functionally complete
    # regardless of how the agent exited, and script quality is the judge's job
    # (Stages 1.6/2.5), not the infra-failure gate's. This applies to BOTH
    # harnesses: the genuinely fatal failures (no output, 0 scripts generated)
    # cannot satisfy passed_cases == total_cases when total_cases > 0, so this
    # never masks a real generation failure. Without it, mini-swe runs that hit
    # the step limit or forgot the completion command would be discarded even
    # when their scripts pass — a large, avoidable loss of usable trials.
    if cc_failure_reason and total_cases > 0 and passed_cases == total_cases:
        cc_failure_reason = None

    write_results(results, passed_cases, total_cases, cc_failure=cc_failure_reason, config=config)

    # --- Stage 2.5: Post-execution unified judge ---
    # Runs after write_results so validation_results.json is available.
    post_judge = run_post_exec_judge(config)
    if post_judge is not None and post_judge.severity.discard_recommended:
        _kill_trial(
            config,
            judge_discard=f"post-exec: {','.join(post_judge.severity.discard_reasons)}",
        )

    if cc_failure_reason:
        sys.exit(2)


def _kill_trial(
    config: ValidationConfig,
    *,
    judge_discard: str | None = None,
    integrity_violation: str | None = None,
) -> None:
    """Write an empty-result validation_results.json and exit(3).

    Used by the three trial-discard paths (pre-retry judge, repo-integrity
    violation, post-execution judge). Exit code 3 → test.sh deletes the
    reward files → harbor surfaces ``RewardFileEmptyError`` downstream.

    Pass exactly one of ``judge_discard`` or ``integrity_violation``.
    """
    write_results(
        [],
        0,
        0,
        judge_discard=judge_discard,
        integrity_violation=integrity_violation,
        config=config,
    )
    sys.exit(3)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Fail CLOSED: an unhandled crash on the validation path (a judge LLM
        # error that survived retries, or check_repo_integrity raising because it
        # could not verify the repo) is an INVALID trial, never a pass. exit 2 →
        # test.sh writes empty reward files → harbor excludes the trial
        # (RewardFileEmptyError). Previously exit(0) let a post-exec-judge crash
        # score the already-written passing validation_results.json (false pass).
        traceback.print_exc(file=sys.stderr)
        sys.exit(2)
