"""Bug-condition exploration test for kiro-agent-engine-flag-detection-fix.

Property 1 (Bug Condition): a legacy wrapper that advertises no
``--agent-engine`` option at all must be treated as a supported legacy
wrapper. Under engine ``v2`` the probe should succeed and the built command
should omit ``--agent-engine`` while still carrying the other flags CAO
requires and the wrapper advertises.

This test encodes the EXPECTED (fixed) behavior. It is written BEFORE the fix
and is EXPECTED TO FAIL on the unfixed code: today ``probe_kiro_capabilities``
raises ``KiroCapabilityError`` for the genuinely-absent ``--agent-engine`` flag,
and ``build_kiro_command`` still emits ``--agent-engine v2``. That failure
confirms the bug exists. Do NOT "fix" this test when it fails.

**Validates: Requirements 1.1, 1.2, 1.3** (bug condition / defect) and the
Expected Behavior (Property 1) Requirements 2.1, 2.2, 2.4 it will confirm once
the fix lands.
"""

import subprocess
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from cli_agent_orchestrator.models.kiro_engine import KiroEngine
from cli_agent_orchestrator.providers.kiro_capabilities import (
    KiroCapabilityError,
    _agent_engines_from_help,
    build_kiro_command,
    probe_kiro_capabilities,
)

_FIXTURES_DIR = Path(__file__).parent / "fixtures"
_LEGACY_ROOT_HELP = (_FIXTURES_DIR / "kiro_cli_1_28_3_root_help.txt").read_text(encoding="utf-8")
_LEGACY_CHAT_HELP = (_FIXTURES_DIR / "kiro_cli_1_28_3_chat_help.txt").read_text(encoding="utf-8")


def _make_runner(root_help: str, chat_help: str, version: str = "1.28.3"):
    """Build a deterministic probe runner for the bounded root/chat/version contract."""

    def _runner(command, **_kwargs):
        if command == ["kiro-cli", "--help"]:
            output = root_help
        elif command == ["kiro-cli", "chat", "--help"]:
            output = chat_help
        else:
            output = f"kiro-cli version {version}"
        return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

    return _runner


# --- Test case 3: Selector genuinely absent (the "genuine absence" signal) ---


def test_agent_engines_from_legacy_chat_help_is_empty():
    """The 1.28.3 chat help declares no ``--agent-engine``: genuine absence signal."""
    assert _agent_engines_from_help(_LEGACY_CHAT_HELP) == frozenset()


# --- Test case 1: Legacy probe rejection (must succeed after fix) ---


def test_probe_v2_against_legacy_wrapper_succeeds_without_agent_engine():
    """Probe v2 against the 1.28.3 fixtures should succeed with empty agent_engines.

    On UNFIXED code this raises KiroCapabilityError(capability="--agent-engine=v2").
    """
    runner = _make_runner(_LEGACY_ROOT_HELP, _LEGACY_CHAT_HELP)

    capabilities = probe_kiro_capabilities(
        KiroEngine.V2,
        {"profile", "trust"},
        runner=runner,
    )

    assert capabilities.version == "1.28.3"
    assert capabilities.agent_engines == frozenset()
    assert capabilities.supports("--agent")
    assert capabilities.supports("--trust-all-tools")


# --- Test case 2: Command must omit the engine flag for a legacy wrapper ---


def test_build_command_unfixed_emits_agent_engine_v2_for_legacy_wrapper():
    """Document the defect: the current builder unconditionally emits --agent-engine v2.

    This encodes the UNFIXED behavior that the fix must change (it carries the
    engine flag even for a legacy wrapper). Once build_kiro_command becomes
    engine-flag aware this expectation flips; the forward-looking assertion lives
    in test_build_command_for_legacy_wrapper_omits_agent_engine below.
    """
    command = build_kiro_command(KiroEngine.V2, "developer", yolo=True)
    assert "--agent-engine" in command
    assert command[command.index("--agent-engine") + 1] == "v2"


def test_build_command_for_legacy_wrapper_omits_agent_engine():
    """For a legacy wrapper build_kiro_command must omit --agent-engine.

    On UNFIXED code build_kiro_command has no legacy-awareness and this call
    raises TypeError (no ``advertises_agent_engine`` parameter yet); the fix
    adds that input and makes the engine flag conditional.
    """
    command = build_kiro_command(
        KiroEngine.V2,
        "developer",
        model="fixture-model",
        yolo=True,
        advertises_agent_engine=False,
    )

    assert "--agent-engine" not in command
    assert command[:2] == ["kiro-cli", "chat"]
    assert "--agent" in command
    assert command[command.index("--agent") + 1] == "developer"
    assert "--trust-all-tools" in command
    assert "--model" in command
    assert command[command.index("--model") + 1] == "fixture-model"
    # The legacy path must never drop the wrapper to v1 via --legacy-ui.
    assert "--legacy-ui" not in command


# --- Test case 4: Edge case — legacy plus genuinely-missing --trust-all-tools ---


def test_probe_v2_legacy_without_trust_still_rejects_trust_flag():
    """A legacy wrapper that also omits --trust-all-tools must still raise for it.

    Omitting --agent-engine is tolerated, but a genuinely-missing required flag
    (here --trust-all-tools under yolo) must continue to be rejected.
    """
    chat_help_without_trust = (
        "AI assistant in your terminal\n\n"
        "Usage: kiro-cli-chat chat [OPTIONS] [INPUT]\n\n"
        "Options:\n"
        "      --agent <AGENT>\n"
        "          Context profile to use\n\n"
        "      --model <MODEL>\n"
        "          Current model to use\n\n"
        "      --legacy-ui\n"
        "          Use the legacy harness\n"
    )
    runner = _make_runner(_LEGACY_ROOT_HELP, chat_help_without_trust)

    with pytest.raises(KiroCapabilityError, match="--trust-all-tools") as exc_info:
        probe_kiro_capabilities(
            KiroEngine.V2,
            {"profile", "trust"},
            runner=runner,
        )

    assert exc_info.value.capability == "--trust-all-tools"


# --- Property: synthetic legacy-help documents that omit --agent-engine ---

# Flags that CAO may request; --agent-engine is deliberately never generated so
# every sampled help document stays in the bug-condition domain (genuine absence).
_OPTIONAL_LEGACY_FLAG_LINES = {
    "model": "      --model <MODEL>\n          Current model to use\n",
    "legacy_ui": "      --legacy-ui\n          Use the legacy harness\n",
    "verbose": "  -v, --verbose...\n          Increase logging verbosity\n",
    "require_mcp": "      --require-mcp-startup\n          Require all enabled MCP servers to start\n",
}


@st.composite
def _legacy_chat_help(draw):
    """Generate chat help that declares --agent and --trust-all-tools but never --agent-engine."""
    extras = draw(
        st.lists(
            st.sampled_from(sorted(_OPTIONAL_LEGACY_FLAG_LINES)),
            unique=True,
            max_size=len(_OPTIONAL_LEGACY_FLAG_LINES),
        )
    )
    lines = [
        "AI assistant in your terminal\n\n",
        "Usage: kiro-cli-chat chat [OPTIONS] [INPUT]\n\n",
        "Options:\n",
        "      --agent <AGENT>\n          Context profile to use\n",
        "  -a, --trust-all-tools\n          Allows the model to use any tool\n",
    ]
    lines.extend(_OPTIONAL_LEGACY_FLAG_LINES[name] for name in extras)
    return "".join(lines)


@settings(max_examples=50, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(chat_help=_legacy_chat_help())
def test_property_legacy_wrapper_probe_succeeds_and_command_omits_engine_flag(chat_help):
    """For any legacy help omitting --agent-engine, v2 probe succeeds and the
    command omits the engine flag (Property 1: Bug Condition / Expected Behavior).

    EXPECTED TO FAIL on unfixed code: the probe raises for the absent selector.
    """
    assert _agent_engines_from_help(chat_help) == frozenset()

    runner = _make_runner(_LEGACY_ROOT_HELP, chat_help)
    capabilities = probe_kiro_capabilities(
        KiroEngine.V2,
        {"profile", "trust"},
        runner=runner,
    )

    assert capabilities.agent_engines == frozenset()

    command = build_kiro_command(
        KiroEngine.V2,
        "developer",
        yolo=True,
        advertises_agent_engine=bool(capabilities.agent_engines),
    )
    assert "--agent-engine" not in command
    assert command[:2] == ["kiro-cli", "chat"]
    assert "--agent" in command
