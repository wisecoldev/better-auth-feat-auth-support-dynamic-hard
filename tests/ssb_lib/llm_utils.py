"""Shared LLM client/creds/model/retry helpers for the reward judges.

Imports no third-party packages at import time (``anthropic`` is imported
lazily inside ``make_client``), so ``import llm_utils`` never fails on a
missing SDK. Reached under both the ``src.python.ssb_lib`` (offline) and
``ssb_lib`` (in-container) namespaces, so intra-package imports stay relative.
"""

from __future__ import annotations

import dataclasses
import os
import time
from typing import Any

# Model defaults. Portkey/Bedrock form is the common case; the plain form is
# for the direct Anthropic API.
JUDGE_MODEL_PORTKEY = "@bedrock/global.anthropic.claude-sonnet-4-6"
JUDGE_MODEL_DIRECT = "claude-sonnet-4-6"

CLASSIFIER_MODEL_PORTKEY = "@bedrock/global.anthropic.claude-haiku-4-5-20251001-v1:0"
CLASSIFIER_MODEL_DIRECT = "claude-haiku-4-5-20251001"

_RETRY_ATTEMPTS = 4

# Substrings marking a transient (retryable) error, matched case-insensitively
# against ``str(exc)``. Kept specific ("rate limit", not bare "rate") to avoid
# retrying real programming errors.
_TRANSIENT_MARKERS = (
    "rate limit",
    "rate_limit",
    "ratelimit",
    "too many requests",
    "overloaded",
    "429",
    "500",
    "502",
    "503",
    "529",
    "timeout",
    "timed out",
    "connection",
    "temporarily",
)


_PORTKEY_BASE_URL = "https://api.portkey.ai"

# Routing key env vars, in precedence order.
_ROUTER_KEY_VARS = ("PORTKEY_API_KEY", "VAL_AGENT_PORTKEY_KEY", "CC_VAL_PORTKEY_KEY")


def resolve_portkey_key() -> str | None:
    """Return the gateway routing key from the first env var that is set."""
    for var in _ROUTER_KEY_VARS:
        if os.environ.get(var):
            return os.environ[var]
    return None


def have_credentials() -> bool:
    """True if any usable credential is present (gateway or direct Anthropic)."""
    return bool(resolve_portkey_key() or os.environ.get("ANTHROPIC_API_KEY"))


def _is_routed() -> bool:
    """True when a gateway routing key is set (vs. direct Anthropic)."""
    return bool(resolve_portkey_key())


def make_client(*, max_retries: int = 3) -> Any | None:
    """Build an Anthropic client, or ``None`` if the SDK or credentials are absent.

    Routing is internal: the gateway (→ Bedrock) when a routing key is set, else
    direct ``ANTHROPIC_API_KEY``. ``max_retries`` is the SDK-level retry count;
    :func:`create_with_retry` / :func:`parse_with_retry` add app-level retries.
    """
    try:
        import anthropic
    except ImportError:
        return None

    key = resolve_portkey_key()
    if key:
        return anthropic.Anthropic(
            api_key="portkey",
            base_url=_PORTKEY_BASE_URL,
            default_headers={"x-portkey-api-key": key},
            max_retries=max_retries,
        )

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    print("WARNING: no routing key set, using direct Anthropic API", flush=True)
    return anthropic.Anthropic(api_key=api_key, max_retries=max_retries)


def judge_model(*, env_var: str | None = "MODEL_NAME") -> str:
    """Resolve the judge model name (gateway vs. direct form chosen internally).

    ``env_var`` lets callers accept an override; pass ``env_var=None`` to pin
    the model (the validation review judge does this — a fixed reviewer).
    """
    default = JUDGE_MODEL_PORTKEY if _is_routed() else JUDGE_MODEL_DIRECT
    if env_var:
        return os.environ.get(env_var, default)
    return default


def classifier_model(*, env_var: str | None = "CLASSIFIER_MODEL_NAME") -> str:
    """Resolve the patch-classifier model name (override via ``env_var``)."""
    default = CLASSIFIER_MODEL_PORTKEY if _is_routed() else CLASSIFIER_MODEL_DIRECT
    if env_var:
        return os.environ.get(env_var, default)
    return default


# The validation agent is a subprocess (Claude Code CLI or mini-swe-agent), not
# an SDK client, so it is routed via environment variables rather than
# make_client(). agent_routing() is the single place that knows how each harness
# reaches the gateway; callers apply the returned env + model fields blindly.


@dataclasses.dataclass
class AgentRouting:
    """Routing config for a validation-agent subprocess.

    ``env`` is merged into the subprocess environment; ``model`` is the slug to
    invoke with; ``model_class`` / ``provider`` are litellm hints (mini-swe-agent
    only). ``label`` is a human tag for logs ("gateway" / "direct").
    """

    model: str
    env: dict[str, str]
    model_class: str = ""
    provider: str = ""
    label: str = "direct"


def _provider_for_model(model: str) -> str:
    """Infer the gateway provider from a gateway-native model slug (``@prov/...``)."""
    if model.startswith("@") and "/" in model:
        return model[1:].split("/", 1)[0]
    return ""


def agent_routing(harness: str, model: str) -> AgentRouting:
    """Resolve subprocess routing for a validation-agent ``harness`` + ``model``.

    ``harness`` is ``"claude_code"`` (routed via ANTHROPIC_* env vars) or
    ``"miniswebench"`` (routed via PORTKEY_API_KEY + litellm model_class/provider).
    With no routing key set, returns direct config (empty routing env).
    """
    key = resolve_portkey_key()
    if not key:
        return AgentRouting(model=model, env={})

    if harness == "claude_code":
        return AgentRouting(
            model=model,
            env={
                "ANTHROPIC_BASE_URL": _PORTKEY_BASE_URL,
                "ANTHROPIC_AUTH_TOKEN": key,
                "ANTHROPIC_CUSTOM_HEADERS": f"x-portkey-api-key: {key}",
            },
            label="gateway",
        )

    # mini-swe-agent (litellm): pass the key through and, for gateway-native
    # slugs, set the portkey model_class + provider.
    model_class = "portkey" if model.startswith("@") else ""
    return AgentRouting(
        model=model,
        env={"PORTKEY_API_KEY": key},
        model_class=model_class,
        provider=_provider_for_model(model) if model_class else "",
        label="gateway",
    )


def _is_transient(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(marker in msg for marker in _TRANSIENT_MARKERS)


def _call_with_retry(fn: Any, attempts: int, kwargs: dict) -> Any:
    """Call ``fn(**kwargs)`` with bounded retry on transient errors.

    Retries up to ``attempts`` times with exponential backoff (1, 2, 4, …
    capped at 30s); non-transient errors raise immediately. If every attempt
    fails the exception propagates.
    """
    last_exc: Exception | None = None
    for attempt in range(attempts):
        try:
            return fn(**kwargs)
        except Exception as e:  # noqa: BLE001 — classify by message; SDK type is Any
            last_exc = e
            if attempt == attempts - 1 or not _is_transient(e):
                raise
            time.sleep(min(2**attempt, 30))
    raise last_exc  # pragma: no cover — loop always returns or raises


def create_with_retry(client: Any, *, _attempts: int = _RETRY_ATTEMPTS, **kwargs: Any) -> Any:
    """``client.messages.create(**kwargs)`` with bounded transient retry."""
    return _call_with_retry(client.messages.create, _attempts, kwargs)


def parse_with_retry(client: Any, *, _attempts: int = _RETRY_ATTEMPTS, **kwargs: Any) -> Any:
    """``client.messages.parse(**kwargs)`` with bounded transient retry."""
    return _call_with_retry(client.messages.parse, _attempts, kwargs)
