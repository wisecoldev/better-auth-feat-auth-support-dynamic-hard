"""Test harness placeholder — this task uses ONLY jest-driver stories.

The jest driver runs vitest on TypeScript files the CC validation agent
writes; it does not invoke any Python code from the validation agent's
side. The only Python validation code that runs for this task is the
orchestrator (`/tests/run_validate.py`) which is shared across all
tasks and does not import this module.

This file exists solely to satisfy `sbgen build --check`, which
currently requires every validation_spec.toml-bearing task to ship a
`test_harness.py` regardless of the actual driver mix.

If the task ever adds a pytest-driver story, replace this placeholder
with real Python harness utilities.
"""
