#!/usr/bin/env python3
# /// script
# dependencies = [
#   "anthropic>=0.40.0",
#   "pydantic>=2.0",
#   "unidiff>=0.7",
#   "pygments>=2.17",
# ]
# ///
"""LLM judges for Harbor benchmark tasks.

Two entry points called by test.sh:
  python3 /tests/run_judge.py rubric  # rubric evaluation → reward.json
  python3 /tests/run_judge.py taste   # taste evaluation → appends to reward.json
"""

import contextlib
import json
import os
import subprocess
import sys
from pathlib import Path

JUDGE_OUTPUT = Path("/logs/verifier/judge_output.json")


def _write_judge_status(key: str, status: str, error: str = "") -> None:
    """Merge a status entry into JUDGE_OUTPUT so failures are observable.

    Every silent-skip / failure path in this module calls this before
    exiting so run_aggregate.py and downstream tooling can distinguish
    "judge ran fine but produced null" from "judge never ran" from
    "judge ran and crashed".
    """
    try:
        JUDGE_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        existing: dict = {}
        if JUDGE_OUTPUT.exists():
            with contextlib.suppress(Exception):
                existing = json.loads(JUDGE_OUTPUT.read_text())
        existing[f"{key}_status"] = status
        if error:
            existing[f"{key}_error"] = error[:500]
        JUDGE_OUTPUT.write_text(json.dumps(existing, indent=2))
    except Exception as exc:
        print(f"Failed to write judge status ({key}={status}): {exc}", file=sys.stderr)


try:
    import anthropic  # noqa: F401 — availability guard; clients are built in llm_utils
    from pydantic import BaseModel, Field
except ImportError:
    print("Missing dependencies (anthropic, pydantic). Skipping judges.", file=sys.stderr)
    _write_judge_status("rubric", "skipped:missing_deps")
    _write_judge_status("taste", "skipped:missing_deps")
    sys.exit(0)

RUBRIC_PATH = Path("/tests/judge/rubric.json")
RUBRIC_ALL_PATH = Path("/tests/judge/rubric_all.json")
RUBRIC_PASS_THRESHOLD = 0.5
_repo_name = os.environ.get("REPO_NAME")
if not _repo_name:
    print("REPO_NAME not set. Skipping LLM judge.", file=sys.stderr)
    _write_judge_status("rubric", "skipped:no_repo_name")
    _write_judge_status("taste", "skipped:no_repo_name")
    sys.exit(0)
REPO_PATH = Path("/repo") / _repo_name


class CriterionScore(BaseModel):
    name: str
    score: float = Field(..., ge=0.0, le=1.0)
    reason: str


class RubricResult(BaseModel):
    criteria: list[CriterionScore]


def parse_rubric(rubric_path: Path) -> tuple[list[str], dict[str, str], dict[str, str]]:
    """Parse rubric.json. Returns (file_paths, fail_to_pass, pass_to_pass)."""
    data = json.loads(rubric_path.read_text())
    files = data.get("files", [])
    fail_to_pass = {c["name"]: c["description"] for c in data.get("fail_to_pass", [])}
    pass_to_pass = {c["name"]: c["description"] for c in data.get("pass_to_pass", [])}
    return files, fail_to_pass, pass_to_pass


def read_file(path: Path) -> str:
    try:
        return path.read_text(errors="replace")
    except OSError as exc:
        return f"[Error reading file: {exc}]"


SUBMIT_SCORES_TOOL = {
    "name": "submit_scores",
    "description": "Submit rubric evaluation scores for each criterion",
    "input_schema": {
        "type": "object",
        "properties": {
            "criteria": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "score": {
                            "type": "number",
                            "minimum": 0.0,
                            "maximum": 1.0,
                        },
                        "reason": {"type": "string"},
                    },
                    "required": ["name", "score", "reason"],
                },
            }
        },
        "required": ["criteria"],
    },
}

RUBRIC_EXPLORE_SYSTEM = (
    "You are evaluating whether an AI agent correctly completed a software "
    "engineering task. You have tools to read files and list directories in "
    "the repository, and submit_scores to record your evaluation.\n\n"
    "The agent may have placed code in different files than "
    "the pre-specified reference paths — use read_file and list_directory to "
    "find and read the actual implementation.\n\n"
    "When you have enough context to score all criteria, call submit_scores."
)


def _build_criteria_block(fail_to_pass: dict[str, str], pass_to_pass: dict[str, str]) -> str:
    """Build the segmented criteria text block for rubric prompts."""
    parts = []
    if fail_to_pass:
        ftp_lines = "\n".join(f"  {i + 1}. [{name}] {desc}" for i, (name, desc) in enumerate(fail_to_pass.items()))
        parts.append(
            "### Fail-to-Pass Criteria\n"
            "These should be FALSE in unmodified code and TRUE only after a correct fix.\n"
            "Score 1.0 if the criterion is satisfied in the current code, 0.0 if not.\n\n" + ftp_lines
        )
    if pass_to_pass:
        ptp_lines = "\n".join(f"  {i + 1}. [{name}] {desc}" for i, (name, desc) in enumerate(pass_to_pass.items()))
        parts.append(
            "### Pass-to-Pass Criteria\n"
            "These should remain TRUE regardless of which fix approach was used.\n"
            "They guard against shortcuts that fix the symptom but degrade quality.\n"
            "Score 1.0 if preserved, 0.0 if violated or degraded.\n\n" + ptp_lines
        )
    return "\n\n".join(parts)


def _make_rubric_client() -> tuple:
    """Create the Anthropic client + model for the rubric judge.

    Credential resolution, client construction (SDK max_retries=3), and model
    defaulting are delegated to the shared ``llm_utils`` module so all three
    judge paths route identically. ``MODEL_NAME`` overrides the model.
    """
    client = llm_utils.make_client(max_retries=3)
    if client is None:
        raise RuntimeError("no LLM credentials / anthropic SDK available for rubric judge")
    return client, llm_utils.judge_model()


def _rubric_explore_and_score(  # noqa: PLR0913
    client,
    model: str,
    criteria_block: str,
    all_names: list[str],
    agent_patch_text: str,
    changed_files: list[str],
    extra_reference_files: list[str],
    repo_name: str,
    error_holder: list[str] | None = None,
) -> RubricResult | None:
    """Run the agentic explore + score flow in a single conversation.

    The judge explores with read_file/list_directory, then scores via
    submit_scores — all in the same message thread so file contents
    read during exploration remain in context for scoring.

    Returns parsed RubricResult or None on failure.
    """
    # Build the agent patch section
    patch_size = len(agent_patch_text.encode("utf-8", errors="replace"))
    if patch_size <= RUBRIC_MAX_PATCH_SIZE:
        patch_section = f"```diff\n{agent_patch_text}\n```"
    else:
        truncated = agent_patch_text[:RUBRIC_MAX_PATCH_SIZE]
        patch_section = (
            f"```diff\n{truncated}\n```\n\n"
            f"... (patch truncated at {RUBRIC_MAX_PATCH_SIZE // 1000}KB, "
            f"{patch_size:,} bytes total. Read changed files directly for full content.)"
        )

    changed_list = "\n".join(f"- {f}" for f in changed_files) or "(none detected)"
    extra_list = "\n".join(f"- {f}" for f in extra_reference_files) if extra_reference_files else "(none)"

    explore_tools = RUBRIC_EXPLORE_TOOLS + [SUBMIT_SCORES_TOOL]

    explore_prompt = (
        f"Evaluate the agent's work in the '{repo_name}' codebase against these criteria.\n\n"
        f"## Agent's Patch\n{patch_section}\n\n"
        f"## Files Changed by the Agent\n{changed_list}\n\n"
        f"## Additional Reference Files\n"
        f"These files from the original codebase may contain relevant context:\n{extra_list}\n\n"
        f"## Rubric Criteria\n{criteria_block}\n\n"
        "Read the changed files and any additional files you need to evaluate these criteria.\n"
        "When you have enough context, call submit_scores with your scores.\n\n"
        f"Use exactly these criterion names: {all_names}"
    )

    messages = [{"role": "user", "content": explore_prompt}]

    for turn in range(RUBRIC_MAX_EXPLORE_TURNS):
        try:
            response = llm_utils.create_with_retry(
                client,
                model=model,
                max_tokens=2048,
                system=RUBRIC_EXPLORE_SYSTEM,
                tools=explore_tools,
                messages=messages,
            )
        except Exception as e:
            print(f"Rubric explore API error on turn {turn}: {e}", file=sys.stderr)
            if error_holder is not None:
                error_holder.append(f"explore turn {turn}: {type(e).__name__}: {e}")
            break

        tool_use_blocks = [b for b in response.content if b.type == "tool_use"]
        n_tools = len(tool_use_blocks)

        # Log each tool call for observability
        for block in tool_use_blocks:
            if block.name == "read_file":
                path = block.input.get("path", "?")
                sl = block.input.get("start_line", "")
                el = block.input.get("end_line", "")
                rng = f" [{sl}-{el}]" if sl or el else ""
                print(f"    read_file: {path}{rng}", file=sys.stderr)
            elif block.name == "list_directory":
                print(f"    list_directory: {block.input.get('path', '?')}", file=sys.stderr)
            elif block.name == "submit_scores":
                print(f"    submit_scores: {len(block.input.get('criteria', []))} criteria", file=sys.stderr)

        print(f"  [rubric turn {turn}] stop={response.stop_reason} tools={n_tools}", file=sys.stderr)

        # Check if the judge called submit_scores
        for block in tool_use_blocks:
            if block.name == "submit_scores":
                return RubricResult.model_validate(block.input)

        # No tool calls at all — judge stopped without scoring
        if not tool_use_blocks and response.stop_reason == "end_turn":
            break

        # Process explore tool calls
        messages.append({"role": "assistant", "content": response.content})
        tool_results: list = []
        for block in tool_use_blocks:
            result_text = _handle_tool_call(block.name, block.input, max_file_lines=RUBRIC_MAX_FILE_LINES)
            tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result_text})

        # On the last couple turns, nudge the judge to score now
        if turn >= RUBRIC_MAX_EXPLORE_TURNS - 2:
            tool_results.append(
                {
                    "type": "text",
                    "text": (
                        "You are running low on exploration turns. Please call submit_scores now with your evaluation."
                    ),
                }
            )
        messages.append({"role": "user", "content": tool_results})

    # Turns exhausted without submit_scores — force a final scoring call
    # within the same conversation that has the full exploration context.
    print("  [rubric] forcing final score call in-context", file=sys.stderr)
    messages.append({"role": "assistant", "content": "I will now score all criteria based on what I've read."})
    messages.append(
        {
            "role": "user",
            "content": (f"Score all criteria now using submit_scores. Use exactly these criterion names: {all_names}"),
        }
    )
    try:
        response = llm_utils.create_with_retry(
            client,
            model=model,
            max_tokens=1024,
            tools=[SUBMIT_SCORES_TOOL],
            tool_choice={"type": "tool", "name": "submit_scores"},
            messages=messages,
        )
        for block in response.content:
            if block.type == "tool_use":
                return RubricResult.model_validate(block.input)
    except Exception as e:
        print(f"Rubric forced score error: {e}", file=sys.stderr)
        if error_holder is not None:
            error_holder.append(f"forced score: {type(e).__name__}: {e}")
    return None


def _rubric_score_single_shot(
    client,
    model: str,
    criteria_block: str,
    all_names: list[str],
    files_block: str,
    repo_name: str,
    error_holder: list[str] | None = None,
) -> RubricResult | None:
    """Original single-shot scoring (no exploration). Used for nop/empty-patch fallback."""
    prompt = (
        f"You are evaluating whether an AI agent correctly completed a "
        f"software engineering task in the '{repo_name}' codebase.\n\n"
        "## Repository files\n\n"
        f"{files_block}\n\n"
        "## Rubric\n\n"
        f"{criteria_block}\n\n"
        f"Use exactly these criterion names: {all_names}"
    )
    try:
        response = llm_utils.create_with_retry(
            client,
            model=model,
            max_tokens=1024,
            tools=[SUBMIT_SCORES_TOOL],
            tool_choice={"type": "tool", "name": "submit_scores"},
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as e:
        print(f"Rubric single-shot API error: {e}", file=sys.stderr)
        if error_holder is not None:
            error_holder.append(f"single-shot: {type(e).__name__}: {e}")
        return None
    for block in response.content:
        if block.type == "tool_use":
            return RubricResult.model_validate(block.input)
    return None


def rubric_main(rubric_path: Path | None = None, output_key: str = "rubric") -> None:  # noqa: PLR0915
    """Run the LLM rubric judge.

    Uses an agentic explore loop when an agent patch is available (the agent
    may have placed code in different files than the rubric's pre-specified
    paths). Falls back to single-shot scoring for nop/empty-patch runs.

    Args:
        rubric_path: Path to the rubric JSON file. Defaults to RUBRIC_PATH.
        output_key: Key in reward.json to write results under. "rubric" for
            the standard rubric, "rubric_all" for the full test-criteria rubric.
    """
    rubric_path = rubric_path or RUBRIC_PATH
    if not llm_utils.have_credentials():
        print(
            "No LLM credentials (PORTKEY_API_KEY / VAL_AGENT_PORTKEY_KEY / ANTHROPIC_API_KEY). Skipping LLM judge.",
            file=sys.stderr,
        )
        _write_judge_status(output_key, "skipped:no_api_key")
        sys.exit(0)

    if not rubric_path.exists():
        print(f"{rubric_path} not found. Skipping LLM judge.", file=sys.stderr)
        _write_judge_status(output_key, "skipped:no_rubric_file")
        sys.exit(0)

    file_paths, fail_to_pass, pass_to_pass = parse_rubric(rubric_path)
    all_criteria = {**fail_to_pass, **pass_to_pass}
    if not all_criteria:
        print("No criteria in rubric.json. Skipping LLM judge.", file=sys.stderr)
        _write_judge_status(output_key, "skipped:no_criteria")
        sys.exit(0)

    criteria_block = _build_criteria_block(fail_to_pass, pass_to_pass)
    all_names = list(all_criteria.keys())
    repo_name = os.environ["REPO_NAME"]
    client, model = _make_rubric_client()

    # Read agent patch to decide agentic vs single-shot
    agent_patch_path = Path("/logs/verifier/agent.patch")
    agent_patch_text = ""
    if agent_patch_path.exists():
        agent_patch_text = agent_patch_path.read_text(errors="replace").strip()

    errors: list[str] = []
    if agent_patch_text:
        # Agentic flow: explore + score in single conversation
        changed_files = _extract_changed_files(agent_patch_text)
        changed_set = set(changed_files)
        extra_reference_files = [p for p in file_paths if p not in changed_set]

        print(
            f"Rubric judge (agentic): {len(changed_files)} changed files, "
            f"{len(extra_reference_files)} extra reference files",
            flush=True,
        )

        result = _rubric_explore_and_score(
            client,
            model,
            criteria_block,
            all_names,
            agent_patch_text,
            changed_files,
            extra_reference_files,
            repo_name,
            error_holder=errors,
        )
    else:
        # Nop / empty-patch fallback: single-shot with pre-specified files
        print("Rubric judge (single-shot): no agent patch, using pre-specified files", flush=True)
        file_sections = []
        for rel_path in file_paths:
            content = read_file(REPO_PATH / rel_path)
            file_sections.append(f"### {rel_path}\n```\n{content}\n```")
        files_block = "\n\n".join(file_sections) or "(no files specified)"

        result = _rubric_score_single_shot(
            client, model, criteria_block, all_names, files_block, repo_name, error_holder=errors
        )

    if result is None:
        if errors:
            print(f"LLM judge failed: {errors[0]}", file=sys.stderr)
            _write_judge_status(output_key, "failed:api_error", "; ".join(errors))
        else:
            print("LLM judge: no tool_use block in response. Skipping.", file=sys.stderr)
            _write_judge_status(output_key, "failed:no_tool_use")
        sys.exit(0)

    scores = {c.name: c.score for c in result.criteria}

    # Compute segmented scores
    ftp_scores = []
    for n in fail_to_pass:
        s = scores.get(n)
        if s is not None:
            ftp_scores.append(s)
        else:
            print(f"  WARNING: criterion '{n}' not returned by judge, skipping from average", file=sys.stderr)

    ptp_scores = []
    for n in pass_to_pass:
        s = scores.get(n)
        if s is not None:
            ptp_scores.append(s)
        else:
            print(f"  WARNING: criterion '{n}' not returned by judge, skipping from average", file=sys.stderr)

    fail_to_pass_score = sum(ftp_scores) / len(ftp_scores) if ftp_scores else None
    pass_to_pass_score = sum(ptp_scores) / len(ptp_scores) if ptp_scores else None
    all_scores = list(scores.values())
    rubric_score = sum(all_scores) / len(all_scores) if all_scores else None

    reward_data: dict = {
        "rubric_score": round(rubric_score, 4) if rubric_score is not None else None,
        "fail_to_pass_score": round(fail_to_pass_score, 4) if fail_to_pass_score is not None else None,
        "pass_to_pass_score": round(pass_to_pass_score, 4) if pass_to_pass_score is not None else None,
    }
    for name in all_names:
        s = scores.get(name)
        reward_data[name] = s if s is not None else None

    JUDGE_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    existing = {}
    if JUDGE_OUTPUT.exists():
        with contextlib.suppress(Exception):
            existing = json.loads(JUDGE_OUTPUT.read_text())
    if output_key == "rubric":
        existing.update(reward_data)
    else:
        existing[output_key] = reward_data
    existing[f"{output_key}_status"] = "ok"
    existing.pop(f"{output_key}_error", None)
    JUDGE_OUTPUT.write_text(json.dumps(existing, indent=2))
    ftp_str = f"{fail_to_pass_score:.2f}" if fail_to_pass_score is not None else "n/a"
    ptp_str = f"{pass_to_pass_score:.2f}" if pass_to_pass_score is not None else "n/a"
    rub_str = f"{rubric_score:.2f}" if rubric_score is not None else "n/a"
    print(
        f"LLM judge complete. "
        f"fail_to_pass={ftp_str} "
        f"pass_to_pass={ptp_str} "
        f"rubric={rub_str} "
        f"({len(all_names)} criteria)"
    )
    for c in result.criteria:
        segment = "F" if c.name in fail_to_pass else "P"
        mark = "+" if c.score >= RUBRIC_PASS_THRESHOLD else "-"
        print(f"  [{segment}{mark}] {c.name}: {c.reason}")


# ---------------------------------------------------------------------------
# Taste evaluation
# ---------------------------------------------------------------------------

AGENT_PATCH = Path("/logs/verifier/agent.patch")

MAX_FILE_LINES = 300
MAX_DIR_ENTRIES = 100
MAX_SEARCH_RESULTS = 50
MIN_EXPLORE_TURNS_BEFORE_ACCEPT = 3

RUBRIC_MAX_EXPLORE_TURNS = 10
RUBRIC_MAX_FILE_LINES = 1000
RUBRIC_MAX_PATCH_SIZE = 50_000
# Oracle patch: prefer /tests/ (available for all runs) over /solution/ (oracle-only)
_ORACLE_IN_TESTS = Path("/tests/judge/oracle.patch")
_ORACLE_IN_SOLUTION = Path("/solution/oracle.patch")
ORACLE_PATCH = _ORACLE_IN_TESTS if _ORACLE_IN_TESTS.exists() else _ORACLE_IN_SOLUTION

MAX_EXPLORE_TURNS = 15


# ---------------------------------------------------------------------------
# Pydantic models for structured output
# ---------------------------------------------------------------------------


class DimensionScore(BaseModel):
    """A single taste dimension score with rationale."""

    score: int = Field(..., ge=1, le=5, description="Score from 1 (worst) to 5 (best)")
    rationale: str = Field(..., description="1-2 sentence justification for the score")


class PracticeAlignment(BaseModel):
    """Practice alignment scores — how well the agent's code fits this codebase."""

    style_consistency: DimensionScore = Field(..., description="Formatting, naming, structure match surrounding code")
    pattern_adherence: DimensionScore = Field(..., description="Uses project's established patterns and idioms")
    library_usage: DimensionScore = Field(
        ..., description="Uses same libraries already in the project (not introducing alternatives)"
    )
    abstraction_level: DimensionScore = Field(..., description="Right abstraction level for this codebase")
    documentation_fit: DimensionScore = Field(..., description="Comments/docstrings match project's style and density")


class RelativeTaste(BaseModel):
    """Relative taste scores — quality compared to oracle (expert human) patch."""

    minimality: DimensionScore = Field(
        ..., description="Changes focused, no scope creep (Agentic Rubrics 'File Change')"
    )
    approach_quality: DimensionScore = Field(
        ...,
        description="Right solution for the problem class — root cause for bugs, "
        "sound design for features, good strategy for migrations",
    )
    hygiene: DimensionScore = Field(
        ...,
        description="No shortcuts, workarounds, test weakening, hardcoded values, "
        "or code smells (Agentic Rubrics 'Integrity')",
    )
    fluency: DimensionScore = Field(
        ...,
        description="Demonstrates understanding of the domain, tools, and conventions — "
        "uses APIs correctly, handles idioms naturally",
    )
    craftsmanship: DimensionScore = Field(
        ...,
        description="Would a senior maintainer approve in code review? "
        "Right abstraction level, right engineering effort",
    )


class TasteScores(BaseModel):
    """Full taste evaluation with practice alignment and relative taste dimensions."""

    practice_alignment: PracticeAlignment
    relative_taste: RelativeTaste


# ---------------------------------------------------------------------------
# Patch SLOC computation — imported from patch_sloc.py (canonical implementation)
# ---------------------------------------------------------------------------

from ssb_lib import llm_utils  # noqa: E402  — shared lib package (tests/ssb_lib, /tests on sys.path)
from ssb_lib.patch_classify import (  # noqa: E402
    FileClassification,
    classify_patch,
    dropped_paths,
    filter_patch_to_behavioral,
)
from ssb_lib.patch_sloc import compute_patch_sloc  # noqa: E402

# Filtered patches written next to AGENT_PATCH so analyze.py / explorer can
# read them the same way they already read agent.patch.
AGENT_PATCH_FILTERED = Path("/logs/verifier/agent_filtered.patch")
ORACLE_PATCH_FILTERED = Path("/logs/verifier/oracle_filtered.patch")


def _make_anthropic_client_and_models() -> tuple[object, str, str] | None:
    """Build a client + taste/classifier model names, or None if unavailable.

    Routing, client construction, and model defaults are delegated to the
    shared ``llm_utils`` module. ``MODEL_NAME`` / ``CLASSIFIER_MODEL_NAME``
    override the respective models.
    """
    client = llm_utils.make_client(max_retries=3)
    if client is None:
        return None
    return client, llm_utils.judge_model(), llm_utils.classifier_model()


# ---------------------------------------------------------------------------
# Patch filtering — symmetric path-aware classifier for both patches.
# Filters out test, doc, and formatting files so the SLOC and minimality
# comparisons against the hand-curated oracle are apples-to-apples. See
# patch_classify.py for the full rationale.
# ---------------------------------------------------------------------------


def _classifications_to_dicts(cls: list[FileClassification]) -> list[dict]:
    return [c.model_dump(mode="json") for c in cls]


def _prepare_filtered_patches(
    agent_text: str,
    oracle_text: str,
    *,
    client: object,
    classifier_model: str,
) -> dict:
    """Classify both patches and return filtered text + audit metadata."""
    agent_cls = classify_patch(agent_text, client=client, model=classifier_model) if agent_text.strip() else []
    oracle_cls = classify_patch(oracle_text, client=client, model=classifier_model) if oracle_text.strip() else []
    return {
        "agent_filtered": filter_patch_to_behavioral(agent_text, agent_cls),
        "oracle_filtered": filter_patch_to_behavioral(oracle_text, oracle_cls),
        "agent_classifications": agent_cls,
        "oracle_classifications": oracle_cls,
        "agent_dropped": dropped_paths(agent_cls),
        "oracle_dropped": dropped_paths(oracle_cls),
    }


# ---------------------------------------------------------------------------
# Assessment 1: Patch Bloat (procedural)
# ---------------------------------------------------------------------------


def assess_patch_bloat(
    agent_text: str,
    oracle_text: str,
    *,
    agent_filtered: str,
    oracle_filtered: str,
    agent_dropped: list[str],
    oracle_dropped: list[str],
) -> dict | None:
    """Compare agent patch SLOC to oracle patch SLOC. No LLM needed.

    Reports both the filtered numbers (authoritative — comparison is fair
    because both sides are reduced to BEHAVIORAL files) and the unfiltered
    numbers (auxiliary signal; useful for spotting classifier regressions).
    """
    if not agent_text.strip() or not oracle_text.strip():
        return None

    agent_stats_unf = compute_patch_sloc(agent_text)
    oracle_stats_unf = compute_patch_sloc(oracle_text)
    agent_stats = compute_patch_sloc(agent_filtered)
    oracle_stats = compute_patch_sloc(oracle_filtered)

    # A patch that failed to parse is UNMEASURED, not a perfect 0-SLOC minimal
    # change — don't emit a (misleadingly excellent) bloat ratio for it. (H11)
    if any(s.get("parse_error") for s in (agent_stats_unf, oracle_stats_unf, agent_stats, oracle_stats)):
        return None

    if oracle_stats["sloc"] == 0:
        # Filtered oracle empty (very small or fully-dropped patch). Fall back
        # to the unfiltered ratio so we still report something useful.
        if oracle_stats_unf["sloc"] == 0:
            return None
        ratio = round(agent_stats_unf["sloc"] / oracle_stats_unf["sloc"], 3)
        ratio_unf = ratio
    else:
        ratio = round(agent_stats["sloc"] / oracle_stats["sloc"], 3)
        ratio_unf = (
            round(agent_stats_unf["sloc"] / oracle_stats_unf["sloc"], 3) if oracle_stats_unf["sloc"] > 0 else None
        )

    return {
        "agent_sloc": agent_stats["sloc"],
        "agent_files": agent_stats["files"],
        "agent_hunks": agent_stats["hunks"],
        "oracle_sloc": oracle_stats["sloc"],
        "oracle_files": oracle_stats["files"],
        "oracle_hunks": oracle_stats["hunks"],
        "bloat_ratio": ratio,
        "agent_sloc_unfiltered": agent_stats_unf["sloc"],
        "agent_files_unfiltered": agent_stats_unf["files"],
        "oracle_sloc_unfiltered": oracle_stats_unf["sloc"],
        "oracle_files_unfiltered": oracle_stats_unf["files"],
        "bloat_ratio_unfiltered": ratio_unf,
        "agent_files_dropped": agent_dropped,
        "oracle_files_dropped": oracle_dropped,
    }


# ---------------------------------------------------------------------------
# Agent exploration tools
# ---------------------------------------------------------------------------

RUBRIC_EXPLORE_TOOLS = [
    {
        "name": "read_file",
        "description": (
            "Read a file from the repository. Returns file content with line numbers. "
            "Use to examine source files for coding patterns, style, conventions."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path relative to repo root."},
                "start_line": {"type": "integer", "description": "Start line (1-based). Optional."},
                "end_line": {"type": "integer", "description": "End line (inclusive). Optional."},
            },
            "required": ["path"],
        },
    },
    {
        "name": "list_directory",
        "description": (
            "List files and directories. Use to discover project structure, find sibling modules, locate config files."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Directory path relative to repo root. '.' for root."},
            },
            "required": ["path"],
        },
    },
]

TASTE_EXPLORE_TOOLS = RUBRIC_EXPLORE_TOOLS + [
    {
        "name": "search_code",
        "description": (
            "Search for a regex pattern in the codebase. Returns matching lines "
            "with file paths. Use to find usage patterns and conventions."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Regex pattern to search for."},
                "file_glob": {"type": "string", "description": "File glob filter (e.g. '*.py'). Optional."},
            },
            "required": ["pattern"],
        },
    },
]


def _handle_tool_call(name: str, input_data: dict, *, max_file_lines: int = MAX_FILE_LINES) -> str:
    """Execute a tool call and return the result as a string."""
    if not REPO_PATH:
        return "[error: REPO_PATH not set]"

    if name == "read_file":
        fpath = REPO_PATH / input_data["path"]
        if not fpath.exists():
            return f"[error: file not found: {input_data['path']}]"
        try:
            content = fpath.read_text(errors="replace")
            lines = content.splitlines()
            start = input_data.get("start_line", 1) - 1
            end = input_data.get("end_line", len(lines))
            selected = lines[max(0, start) : end]
            if len(selected) > max_file_lines:
                selected = selected[:max_file_lines]
                selected.append(f"... (truncated, {len(lines) - max_file_lines} more lines)")
            return "\n".join(f"{start + i + 1}: {line}" for i, line in enumerate(selected))
        except Exception as e:
            return f"[error reading file: {e}]"

    elif name == "list_directory":
        dpath = REPO_PATH / input_data["path"]
        if not dpath.exists():
            return f"[error: directory not found: {input_data['path']}]"
        try:
            entries = sorted(dpath.iterdir())
            result = []
            for entry in entries[:MAX_DIR_ENTRIES]:
                prefix = "d " if entry.is_dir() else "f "
                result.append(prefix + entry.name)
            if len(entries) > MAX_DIR_ENTRIES:
                result.append(f"... ({len(entries) - MAX_DIR_ENTRIES} more entries)")
            return "\n".join(result)
        except Exception as e:
            return f"[error listing directory: {e}]"

    elif name == "search_code":
        pattern = input_data["pattern"]
        file_glob = input_data.get("file_glob", "")
        try:
            cmd = (
                ["grep", "-rn", "--include", file_glob, pattern, str(REPO_PATH)]
                if file_glob
                else ["grep", "-rn", pattern, str(REPO_PATH)]
            )
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10, check=False)
            lines = result.stdout.splitlines()[:MAX_SEARCH_RESULTS]
            prefix = str(REPO_PATH) + "/"
            cleaned = [ln.replace(prefix, "", 1) for ln in lines]
            if len(result.stdout.splitlines()) > MAX_SEARCH_RESULTS:
                cleaned.append(f"... ({len(result.stdout.splitlines()) - MAX_SEARCH_RESULTS} more matches)")
            return "\n".join(cleaned) if cleaned else "(no matches)"
        except Exception as e:
            return f"[error searching: {e}]"

    return f"[unknown tool: {name}]"


# ---------------------------------------------------------------------------
# Assessment 2 & 3: Agent-judged taste (two-phase)
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are a senior code reviewer evaluating an AI coding agent's patch.

You have tools to explore the codebase autonomously. Your goal is to deeply understand
the project's conventions before grading. Explore systematically:

1. **Developer guides & standards**: Look for CONTRIBUTING.md, STYLE_GUIDE.md,
   .editorconfig, pyproject.toml [tool.ruff], .eslintrc, .prettierrc, tox.ini,
   setup.cfg, or any docs/ directory with coding standards. List the repo root first.

2. **Surrounding code context**: Read sibling files in the same directory as the
   changed files. Look at parent package __init__.py files.

3. **Similar patterns elsewhere**: Search for how the codebase handles similar
   concerns (error handling, class patterns, etc.)

4. **Project structure**: List key directories to understand the architecture.

IMPORTANT: If context near the changed files is thin, actively search the broader
codebase for relevant examples. You need to understand the project to judge how
well the agent's code would blend in.

When you have enough context, respond with "READY TO SCORE" and a brief summary
of the codebase conventions you observed. Do NOT score yet — the scoring will
happen in a separate step with structured output."""

SCORING_SYSTEM = """You are a senior code reviewer. Based on your exploration of the codebase,
score the agent's patch on all 10 dimensions below.

## Practice Alignment (1-5 scale)
1 = Violates codebase norms, 3 = Adequate, 5 = Indistinguishable from maintainer code
- style_consistency: Formatting, naming, structure match
- pattern_adherence: Uses project's established patterns
- library_usage: Uses same libraries (not introducing alternatives)
- abstraction_level: Right abstraction for this codebase
- documentation_fit: Comments/docstrings match project style

## Relative Taste vs Oracle (1-5 scale)
1 = Much worse than oracle, 3 = Comparable, 5 = Superior to oracle
- minimality: Changes focused, no scope creep
- approach_quality: Right solution for this problem class (root cause for bugs, sound design for features)
- hygiene: No shortcuts, workarounds, test weakening, hardcoded values
- fluency: Understands domain, tools, conventions — uses APIs correctly
- craftsmanship: Senior maintainer would approve without changes

Your response MUST be valid JSON matching the TasteScores schema. Each dimension
needs a score (1-5 integer) and rationale (1-2 sentence string)."""


def _extract_changed_files(patch_text: str) -> list[str]:
    """Extract file paths from a unified diff."""
    paths = []
    for line in patch_text.splitlines():
        if line.startswith("+++ b/"):
            paths.append(line[6:])
        elif line.startswith("+++ ") and "/dev/null" not in line:
            paths.append(line[4:].strip())
    return paths


def assess_taste_with_llm(
    agent_patch: str,
    oracle_patch: str,
    *,
    client: object,
    model: str,
) -> dict | None:
    """Run two-phase taste evaluation: explore codebase, then score with structured output.

    The agent and oracle patches passed here should already be filtered to
    behavioral files only — see _prepare_filtered_patches in taste_main.
    Filtering before this call keeps the LLM's minimality / craftsmanship
    judgments focused on the same scope as the hand-curated oracle.
    """
    changed_files = _extract_changed_files(agent_patch)
    files_list = ", ".join(changed_files[:10]) if changed_files else "(unknown)"

    # ── Phase 1: Agentic exploration ─────────────────────────────
    explore_prompt = (
        f"Evaluate this agent's patch against the codebase's practices and the oracle patch.\n\n"
        f"## Agent's Patch\n```diff\n{agent_patch[:12000]}\n```\n\n"
        f"## Oracle Patch (expert human baseline)\n```diff\n{oracle_patch[:12000]}\n```\n\n"
        f"Files modified: {files_list}\n\n"
        f"Explore the codebase around the changed files to understand conventions. "
        f"When done, respond with 'READY TO SCORE' and a summary of what you found."
    )

    messages = [{"role": "user", "content": explore_prompt}]
    exploration_notes = ""

    for turn in range(MAX_EXPLORE_TURNS):
        try:
            response = llm_utils.create_with_retry(
                client,
                model=model,
                max_tokens=4096,
                system=SYSTEM_PROMPT,
                tools=TASTE_EXPLORE_TOOLS,
                messages=messages,
            )
        except Exception as e:
            print(f"Taste agent API error on turn {turn}: {e}", file=sys.stderr)
            return None

        tool_use_blocks = [b for b in response.content if b.type == "tool_use"]
        text_blocks = [b.text for b in response.content if b.type == "text"]
        print(f"  [taste turn {turn}] stop={response.stop_reason} tools={len(tool_use_blocks)}", file=sys.stderr)

        # Check if model is ready to score (said "READY TO SCORE" or similar)
        full_text = " ".join(text_blocks)
        if "READY TO SCORE" in full_text.upper() or (not tool_use_blocks and turn >= MIN_EXPLORE_TURNS_BEFORE_ACCEPT):
            exploration_notes = full_text
            break

        # No tool calls — nudge or accept
        if not tool_use_blocks:
            if response.stop_reason == "end_turn":
                if turn < MIN_EXPLORE_TURNS_BEFORE_ACCEPT:
                    messages.append({"role": "assistant", "content": response.content})
                    messages.append({"role": "user", "content": "Please explore the codebase first using the tools."})
                    continue
                else:
                    exploration_notes = full_text
                    break
            break

        # Process tool calls
        messages.append({"role": "assistant", "content": response.content})
        tool_results = []
        for block in tool_use_blocks:
            result_text = _handle_tool_call(block.name, block.input)
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result_text,
                }
            )
        messages.append({"role": "user", "content": tool_results})

        # Nudge when running low
        if turn >= MAX_EXPLORE_TURNS - 2:
            messages.append(
                {
                    "role": "user",
                    "content": "Time to wrap up. Respond with 'READY TO SCORE' and your codebase observations.",
                }
            )

    # ── Phase 2: Structured scoring ──────────────────────────────
    scoring_prompt = (
        f"Based on your exploration, score the agent's patch.\n\n"
        f"## Codebase Observations\n{exploration_notes}\n\n"
        f"## Agent's Patch\n```diff\n{agent_patch[:12000]}\n```\n\n"
        f"## Oracle Patch (expert human baseline)\n```diff\n{oracle_patch[:12000]}\n```\n\n"
        f"Score all 10 dimensions. Each needs a score (1-5) and rationale (1-2 sentences)."
    )

    try:
        scored = llm_utils.parse_with_retry(
            client,
            model=model,
            max_tokens=4096,
            system=SCORING_SYSTEM,
            messages=[{"role": "user", "content": scoring_prompt}],
            output_format=TasteScores,
        )
    except Exception as e:
        print(f"Taste scoring API error: {e}", file=sys.stderr)
        return None

    if scored.parsed_output is None:
        print(f"Taste scoring: no parsed output (stop={scored.stop_reason})", file=sys.stderr)
        return None

    return _flatten_taste_scores(scored.parsed_output)


def _flatten_taste_scores(scores: TasteScores) -> dict:
    """Flatten validated TasteScores into a result dict."""
    pa = scores.practice_alignment
    rt = scores.relative_taste

    pa_dims = {
        "style_consistency": pa.style_consistency,
        "pattern_adherence": pa.pattern_adherence,
        "library_usage": pa.library_usage,
        "abstraction_level": pa.abstraction_level,
        "documentation_fit": pa.documentation_fit,
    }
    rt_dims = {
        "minimality": rt.minimality,
        "approach_quality": rt.approach_quality,
        "hygiene": rt.hygiene,
        "fluency": rt.fluency,
        "craftsmanship": rt.craftsmanship,
    }

    pa_mean = round(sum(d.score for d in pa_dims.values()) / len(pa_dims), 2)
    rt_mean = round(sum(d.score for d in rt_dims.values()) / len(rt_dims), 2)

    result = {
        "practice_alignment_score": pa_mean,
        "relative_taste_score": rt_mean,
    }
    for k, d in pa_dims.items():
        result[f"pa_{k}"] = d.score
        result[f"pa_{k}_rationale"] = d.rationale
    for k, d in rt_dims.items():
        result[f"rt_{k}"] = d.score
        result[f"rt_{k}_rationale"] = d.rationale

    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def taste_main():
    existing = {}
    if JUDGE_OUTPUT.exists():
        with contextlib.suppress(Exception):
            existing = json.loads(JUDGE_OUTPUT.read_text())

    agent_patch = AGENT_PATCH.read_text(errors="replace") if AGENT_PATCH.exists() else ""
    oracle_patch = ORACLE_PATCH.read_text(errors="replace") if ORACLE_PATCH.exists() else ""

    if not (agent_patch.strip() or oracle_patch.strip()):
        _write_judge_status("taste", "skipped:no_patches")
        print("Taste skipped: neither agent nor oracle patch present.", file=sys.stderr)
        return

    # Build LLM client once and use it for both classification and taste scoring.
    client_and_models = _make_anthropic_client_and_models()

    if client_and_models is not None:
        client, taste_model, classifier_model = client_and_models
        prepared = _prepare_filtered_patches(
            agent_patch, oracle_patch, client=client, classifier_model=classifier_model
        )
        agent_filtered = prepared["agent_filtered"]
        oracle_filtered = prepared["oracle_filtered"]
        agent_cls = prepared["agent_classifications"]
        oracle_cls = prepared["oracle_classifications"]
        agent_dropped = prepared["agent_dropped"]
        oracle_dropped = prepared["oracle_dropped"]

        # Persist filtered patches next to the raw ones so analyze.py and the
        # explorer can pick them up the same way they read agent.patch today.
        try:
            AGENT_PATCH_FILTERED.parent.mkdir(parents=True, exist_ok=True)
            AGENT_PATCH_FILTERED.write_text(agent_filtered)
            ORACLE_PATCH_FILTERED.write_text(oracle_filtered)
        except OSError as exc:
            print(f"Failed to write filtered patches: {exc}", file=sys.stderr)

        existing["patch_classifications"] = {
            "agent": _classifications_to_dicts(agent_cls),
            "oracle": _classifications_to_dicts(oracle_cls),
        }
    else:
        # No LLM credentials — skip filtering. Bloat ratio falls back to
        # unfiltered numbers, taste judgment is skipped entirely.
        client = None
        taste_model = ""
        agent_filtered = agent_patch
        oracle_filtered = oracle_patch
        agent_dropped = []
        oracle_dropped = []
        existing["taste_status"] = "skipped:no_api_key"
        print("Patch classifier skipped (no API credentials); using unfiltered patches.", file=sys.stderr)

    # Assessment 1: Patch bloat (procedural)
    bloat = assess_patch_bloat(
        agent_patch,
        oracle_patch,
        agent_filtered=agent_filtered,
        oracle_filtered=oracle_filtered,
        agent_dropped=agent_dropped,
        oracle_dropped=oracle_dropped,
    )
    if bloat:
        existing["patch_bloat"] = bloat
        unf = bloat.get("bloat_ratio_unfiltered")
        unf_str = f" (unfiltered ratio={unf})" if unf is not None and unf != bloat["bloat_ratio"] else ""
        dropped_count = len(bloat.get("agent_files_dropped", []))
        dropped_str = f", {dropped_count} agent files dropped" if dropped_count else ""
        print(
            f"Patch bloat: agent={bloat['agent_sloc']} SLOC / oracle={bloat['oracle_sloc']} SLOC "
            f"→ ratio={bloat['bloat_ratio']}{unf_str}{dropped_str}"
        )

    # Assessments 2 & 3: Agent-judged taste (explores codebase autonomously)
    if client is not None and agent_filtered.strip() and oracle_filtered.strip():
        taste = assess_taste_with_llm(agent_filtered, oracle_filtered, client=client, model=taste_model)
        if taste:
            existing["taste"] = {k: v for k, v in taste.items() if not k.endswith("_rationale")}
            existing["taste_rationales"] = {k: v for k, v in taste.items() if k.endswith("_rationale")}
            existing["taste_status"] = "ok"
            existing.pop("taste_error", None)
            print(
                f"Taste: practice_alignment={taste.get('practice_alignment_score', 'N/A')}/5 "
                f"relative_taste={taste.get('relative_taste_score', 'N/A')}/5"
            )
            for prefix, label in [("pa_", "Practice"), ("rt_", "Taste")]:
                for k, v in taste.items():
                    if (
                        k.startswith(prefix)
                        and not k.endswith("_rationale")
                        and k not in ("practice_alignment_score", "relative_taste_score")
                    ):
                        dim_name = k[len(prefix) :]
                        rationale = taste.get(f"{k}_rationale", "")
                        print(f"  [{label}] {dim_name}={v}/5: {rationale}")
        else:
            existing["taste_status"] = "failed:api_error"
    elif client is None and "taste_status" not in existing:
        existing["taste_status"] = "skipped:no_api_key"
    elif client is not None and not (agent_filtered.strip() and oracle_filtered.strip()):
        existing["taste_status"] = "skipped:empty_patches"

    JUDGE_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    JUDGE_OUTPUT.write_text(json.dumps(existing, indent=2))
    print("Taste evaluation complete.")


# ---------------------------------------------------------------------------
# CLI dispatch
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    _STATUS_KEY = {"rubric": "rubric", "rubric-all": "rubric_all", "taste": "taste"}.get(cmd)
    try:
        if cmd == "rubric":
            rubric_main()
        elif cmd == "rubric-all":
            rubric_main(rubric_path=RUBRIC_ALL_PATH, output_key="rubric_all")
        elif cmd == "taste":
            taste_main()
        else:
            print(f"Usage: {sys.argv[0]} <rubric|rubric-all|taste>", file=sys.stderr)
            sys.exit(1)
    except SystemExit:
        raise
    except Exception as exc:
        import traceback as _tb

        print(f"Judge failed: {exc}", file=sys.stderr)
        _tb.print_exc(file=sys.stderr)
        if _STATUS_KEY:
            _write_judge_status(_STATUS_KEY, "failed:uncaught_exception", f"{type(exc).__name__}: {exc}")
        sys.exit(0)
