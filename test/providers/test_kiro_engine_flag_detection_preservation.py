"""Preservation property tests for kiro-agent-engine-flag-detection-fix.

Property 2 (Preservation): every input where the bug condition does NOT hold
must behave exactly as before. The bug condition is "resolved engine is ``v2``
AND the wrapper advertises no ``--agent-engine`` option at all" — so a
non-bug-condition input is any wrapper that advertises ``--agent-engine`` (with
or without ``v2``), any ``kas`` launch, any execution failure, any other
genuinely-missing required flag, or a flag mentioned only in prose.

These tests follow the observation-first methodology: each assertion records the
behavior ``probe_kiro_capabilities`` / ``build_kiro_command`` exhibit on the
UNFIXED code for a non-bug-condition input. They are written BEFORE the fix and
MUST PASS on the unfixed code — that is what makes them a baseline. After the fix
lands (task 3.3) the SAME tests are re-run and must still pass, proving the fix
introduced no regression on any non-bug-condition input.

Because the fix adds an ``advertises_agent_engine`` keyword to
``build_kiro_command`` (absent on the unfixed code), the command-shape
assertions below call the builder through ``_build_command`` which supplies that
keyword only when the running code accepts it. On the unfixed code the keyword is
omitted (the builder unconditionally emits ``--agent-engine v2`` for an
advertised-``v2`` wrapper); on the fixed code the keyword is supplied as
``True`` for an advertised wrapper, which must reproduce the identical argv.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6**
"""

import inspect
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
_MODERN_ROOT_HELP = (_FIXTURES_DIR / "kiro_cli_2_13_0_root_help.txt").read_text(encoding="utf-8")
_MODERN_CHAT_HELP = (_FIXTURES_DIR / "kiro_cli_2_13_0_chat_help.txt").read_text(encoding="utf-8")

# Whether the running code's build_kiro_command accepts the fix's
# ``advertises_agent_engine`` keyword. False on the unfixed code, True after the
# fix. Lets one test file assert the SAME command shape across both phases.
_BUILDER_IS_ENGINE_FLAG_AWARE = (
    "advertises_agent_engine" in inspect.signature(build_kiro_command).parameters
)


def _build_command(engine, agent_profile, *, model=None, yolo=False, advertises_agent_engine):
    """Call build_kiro_command with the fix keyword only when it is supported.

    On the unfixed builder (no ``advertises_agent_engine`` parameter) the keyword
    is dropped; the unfixed v2 path always emits ``--agent-engine v2``, which is
    exactly what every non-bug-condition caller here expects. On the fixed
    builder the keyword is forwarded so an advertised wrapper reproduces the same
    argv.
    """
    if _BUILDER_IS_ENGINE_FLAG_AWARE:
        return build_kiro_command(
            engine,
            agent_profile,
            model=model,
            yolo=yolo,
            advertises_agent_engine=advertises_agent_engine,
        )
    return build_kiro_command(engine, agent_profile, model=model, yolo=yolo)


def _make_runner(root_help: str, chat_help: str, version: str = "2.13.0"):
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


# =====================================================================
# Observed baseline — concrete non-bug-condition cases (design Test Cases 1-6)
# =====================================================================


# --- Test case 1: Advertised v2 preserved (the 2.13.0 fixture) ---


def test_modern_fixture_probes_successfully_and_advertises_v2():
    """Observed: the 2.13.0 fixture probes successfully and advertises v2.

    This is NOT a bug-condition input (``--agent-engine`` is advertised), so the
    probe must behave identically before and after the fix.
    """
    runner = _make_runner(_MODERN_ROOT_HELP, _MODERN_CHAT_HELP)

    capabilities = probe_kiro_capabilities(
        KiroEngine.V2,
        {"profile", "model", "ui", "trust", "mcp_startup"},
        runner=runner,
    )

    assert capabilities.version == "2.13.0"
    assert capabilities.agent_engines == frozenset({"v1", "v2", "v3"})
    assert capabilities.supports_agent_engine(KiroEngine.V2)


def test_modern_wrapper_command_carries_agent_engine_v2():
    """Observed: an advertised-v2 wrapper builds ``kiro-cli chat --agent-engine v2 …``.

    The 2.13.0 wrapper advertises ``--agent-engine`` accepting ``v2`` so this is a
    non-bug-condition input; the argv must be byte-for-byte preserved.
    """
    command = _build_command(
        KiroEngine.V2,
        "developer",
        model="fixture-model",
        yolo=True,
        advertises_agent_engine=True,
    )

    assert command == [
        "kiro-cli",
        "chat",
        "--agent-engine",
        "v2",
        "--trust-all-tools",
        "--model",
        "fixture-model",
        "--agent",
        "developer",
    ]


# --- Test case 2: Advertised-without-v2 still rejected ---


def test_advertised_without_v2_still_raises_agent_engine_v2():
    """Observed: a wrapper declaring ``--agent-engine v1|v3`` (no v2) is rejected.

    The selector IS advertised (so this is a non-bug-condition input); the probe
    must keep raising ``KiroCapabilityError(capability="--agent-engine=v2")``.
    """
    chat_help = (
        "Usage: kiro-cli-chat chat [OPTIONS] [INPUT]\n\n"
        "Options:\n"
        "      --agent <AGENT>\n          Context profile to use\n"
        "  -a, --trust-all-tools\n          Allows the model to use any tool\n"
        "      --agent-engine v1|v3\n          Agent engine to use\n"
    )
    runner = _make_runner(_MODERN_ROOT_HELP, chat_help)

    with pytest.raises(KiroCapabilityError, match="accept 'v2'") as exc_info:
        probe_kiro_capabilities(
            KiroEngine.V2,
            {"profile", "trust"},
            runner=runner,
        )

    assert exc_info.value.kind == "unsupported_capability"
    assert exc_info.value.capability == "--agent-engine=v2"


# --- Test case 3: kas requires --v3; command shape unchanged ---


def test_kas_without_v3_still_raises_v3_capability_error():
    """Observed: a ``kas`` launch against help lacking ``--v3`` raises for ``--v3``."""
    chat_help = (
        "Usage: kiro-cli-chat chat [OPTIONS] [INPUT]\n\n"
        "Options:\n"
        "      --agent <AGENT>\n          Context profile to use\n"
    )
    runner = _make_runner(_MODERN_ROOT_HELP, chat_help)

    with pytest.raises(KiroCapabilityError, match="--v3") as exc_info:
        probe_kiro_capabilities(KiroEngine.KAS, {"profile"}, runner=runner)

    assert exc_info.value.kind == "unsupported_capability"
    assert exc_info.value.engine == KiroEngine.KAS
    assert exc_info.value.capability == "--v3"


def test_kas_command_shape_is_unchanged():
    """Observed: the ``kas`` command is ``kiro-cli --v3 chat … --agent <profile>``."""
    command = _build_command(
        KiroEngine.KAS,
        "developer",
        advertises_agent_engine=False,
    )

    assert command == ["kiro-cli", "--v3", "chat", "--agent", "developer"]


# --- Test case 4: other genuinely-missing required flag preserved ---


def test_missing_trust_all_tools_under_yolo_still_raises():
    """Observed: an advertised-v2 wrapper lacking ``--trust-all-tools`` raises for it.

    ``--agent-engine`` is advertised (not a bug-condition input), and the
    genuinely-missing ``--trust-all-tools`` under yolo must still be rejected.
    """
    chat_help = (
        "Usage: kiro-cli-chat chat [OPTIONS] [INPUT]\n\n"
        "Options:\n"
        "      --agent <AGENT>\n          Context profile to use\n"
        "      --agent-engine <ENGINE>\n"
        "          Agent engine to use\n\n"
        "          [possible values: v2, v1, v3]\n"
    )
    runner = _make_runner(_MODERN_ROOT_HELP, chat_help)

    with pytest.raises(KiroCapabilityError, match="--trust-all-tools") as exc_info:
        probe_kiro_capabilities(
            KiroEngine.V2,
            {"profile", "trust"},
            runner=runner,
        )

    assert exc_info.value.capability == "--trust-all-tools"


# --- Test case 5: execution failures preserved ---


@pytest.mark.parametrize(
    ("runner", "kind"),
    [
        (
            lambda *_args, **_kwargs: (_ for _ in ()).throw(FileNotFoundError()),
            "missing_executable",
        ),
        (
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                subprocess.TimeoutExpired("kiro-cli", 1)
            ),
            "timeout",
        ),
        (
            lambda command, **_kwargs: subprocess.CompletedProcess(
                command, 0, stdout="", stderr=""
            ),
            "malformed_output",
        ),
    ],
)
def test_execution_failures_preserve_their_kinds(runner, kind):
    """Observed: missing executable / timeout / malformed output raise their kinds."""
    with pytest.raises(KiroCapabilityError) as exc_info:
        probe_kiro_capabilities(KiroEngine.V2, set(), runner=runner)

    assert exc_info.value.kind == kind


# --- Test case 6: prose-only flag stays not advertised ---


def test_prose_only_agent_engine_is_not_advertised():
    """Observed: ``--agent-engine`` mentioned only in prose yields an empty selector."""
    prose_only = "Use --agent-engine <ENGINE> to pick the engine; possible values: v2, v1, v3.\n"

    assert _agent_engines_from_help(prose_only) == frozenset()


def test_prose_only_required_flag_still_raises():
    """Observed: a ``--trust-all-tools`` mentioned only in prose is treated as absent."""
    chat_help = (
        "Usage: kiro-cli-chat chat [OPTIONS] [INPUT]\n\n"
        "Options:\n"
        "      --agent <AGENT>\n          Context profile to use\n"
        "      --agent-engine <ENGINE>\n"
        "          Agent engine to use\n\n"
        "          [possible values: v2, v1, v3]\n\n"
        "Use --trust-all-tools to bypass confirmation.\n"
    )
    runner = _make_runner(_MODERN_ROOT_HELP, chat_help)

    with pytest.raises(KiroCapabilityError, match="--trust-all-tools") as exc_info:
        probe_kiro_capabilities(
            KiroEngine.V2,
            {"profile", "trust"},
            runner=runner,
        )

    assert exc_info.value.capability == "--trust-all-tools"


# =====================================================================
# Property-based preservation — help-shaped inputs across the domain
# =====================================================================

# A formal --agent-engine declaration line keyed by the value set it advertises.
# Every value set here is non-empty, so each generated help document advertises
# the selector: that keeps every sampled input OUT of the bug-condition domain
# (genuine absence) and inside the Preservation domain.
_ENGINE_VALUE_SETS = {
    (
        "v2",
    ): "      --agent-engine <ENGINE>\n          Agent engine\n\n          [possible values: v2]\n",
    (
        "v1",
        "v2",
        "v3",
    ): "      --agent-engine <ENGINE>\n          Agent engine\n\n          [possible values: v2, v1, v3]\n",
    (
        "v2",
        "v3",
    ): "      --agent-engine <ENGINE>\n          Agent engine\n\n          [possible values: v2, v3]\n",
    (
        "v1",
    ): "      --agent-engine <ENGINE>\n          Agent engine\n\n          [possible values: v1]\n",
    (
        "v1",
        "v3",
    ): "      --agent-engine <ENGINE>\n          Agent engine\n\n          [possible values: v1, v3]\n",
    (
        "v3",
    ): "      --agent-engine <ENGINE>\n          Agent engine\n\n          [possible values: v3]\n",
}

# Optional flags CAO may request; each as a formal declaration line.
_OPTIONAL_FLAG_LINES = {
    "model": "      --model <MODEL>\n          Current model to use\n",
    "legacy_ui": "      --legacy-ui\n          Use the legacy harness\n",
    "require_mcp": "      --require-mcp-startup\n          Require MCP startup\n",
    "verbose": "  -v, --verbose...\n          Increase logging verbosity\n",
}


@st.composite
def _advertised_chat_help(draw):
    """Generate chat help that always declares --agent-engine with some value set.

    Returns ``(help_text, engine_values, has_trust)``. The declared selector keeps
    every example a non-bug-condition (Preservation) input. ``--trust-all-tools``
    is included or omitted so the probe's trust enforcement can be exercised.
    """
    engine_values = draw(st.sampled_from(sorted(_ENGINE_VALUE_SETS)))
    has_trust = draw(st.booleans())
    extras = draw(
        st.lists(
            st.sampled_from(sorted(_OPTIONAL_FLAG_LINES)),
            unique=True,
            max_size=len(_OPTIONAL_FLAG_LINES),
        )
    )

    lines = [
        "AI assistant in your terminal\n\n",
        "Usage: kiro-cli-chat chat [OPTIONS] [INPUT]\n\n",
        "Options:\n",
        "      --agent <AGENT>\n          Context profile to use\n",
        _ENGINE_VALUE_SETS[engine_values],
    ]
    if has_trust:
        lines.append("  -a, --trust-all-tools\n          Allows the model to use any tool\n")
    lines.extend(_OPTIONAL_FLAG_LINES[name] for name in extras)
    return "".join(lines), frozenset(engine_values), has_trust


@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(data=_advertised_chat_help())
def test_property_advertised_selector_probe_matches_v2_membership(data):
    """For any help that advertises --agent-engine, the v2 probe succeeds iff the
    selector includes v2, and rejects with --agent-engine=v2 otherwise.

    Preservation: these are all non-bug-condition inputs (selector advertised), so
    the unfixed behavior is the baseline and the fixed code must match it.
    """
    chat_help, engine_values, has_trust = data

    # Every generated help advertises the selector: non-empty genuine selector.
    assert _agent_engines_from_help(chat_help) == engine_values
    assert engine_values != frozenset()

    requested = {"profile"}
    if has_trust:
        requested.add("trust")

    if "v2" in engine_values:
        capabilities = probe_kiro_capabilities(
            KiroEngine.V2,
            requested,
            runner=_make_runner(_MODERN_ROOT_HELP, chat_help),
        )
        assert capabilities.agent_engines == engine_values
        assert capabilities.supports_agent_engine(KiroEngine.V2)

        command = _build_command(
            KiroEngine.V2,
            "developer",
            yolo=has_trust,
            advertises_agent_engine=True,
        )
        # Advertised v2 → command carries --agent-engine v2.
        assert "--agent-engine" in command
        assert command[command.index("--agent-engine") + 1] == "v2"
        assert command[:2] == ["kiro-cli", "chat"]
        assert "--agent" in command
    else:
        with pytest.raises(KiroCapabilityError) as exc_info:
            probe_kiro_capabilities(
                KiroEngine.V2,
                requested,
                runner=_make_runner(_MODERN_ROOT_HELP, chat_help),
            )
        assert exc_info.value.capability == "--agent-engine=v2"


@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    engine_values=st.sampled_from(sorted(_ENGINE_VALUE_SETS)),
    request_trust=st.booleans(),
)
def test_property_missing_trust_rejected_regardless_of_selector(engine_values, request_trust):
    """For any advertised selector, a wrapper that omits a genuinely-required flag
    is rejected, and a fully-satisfied v2 wrapper probes successfully.

    Observed ordering on the baseline: the generic required-flag loop runs before
    the ``--agent-engine=v2`` selector check, so when ``--trust-all-tools`` is
    requested (yolo) but genuinely absent, the probe raises ``--trust-all-tools``
    FIRST — even when the selector also lacks ``v2``. Only when trust is not
    requested does the selector-value rejection (``--agent-engine=v2``) surface.

    Preservation: the selector is always advertised, so no example is a
    bug-condition input; behavior must be identical before and after the fix.
    """
    chat_help = (
        "Usage: kiro-cli-chat chat [OPTIONS] [INPUT]\n\n"
        "Options:\n"
        "      --agent <AGENT>\n          Context profile to use\n"
        + _ENGINE_VALUE_SETS[engine_values]
    )
    requested = {"profile"}
    if request_trust:
        requested.add("trust")
    runner = _make_runner(_MODERN_ROOT_HELP, chat_help)

    values = frozenset(engine_values)
    if request_trust:
        # --trust-all-tools is genuinely absent from the help; the required-flag
        # loop rejects it before the selector-value check is ever reached.
        with pytest.raises(KiroCapabilityError) as exc_info:
            probe_kiro_capabilities(KiroEngine.V2, requested, runner=runner)
        assert exc_info.value.capability == "--trust-all-tools"
    elif "v2" not in values:
        # No other required flag missing → the selector-value check rejects v2.
        with pytest.raises(KiroCapabilityError) as exc_info:
            probe_kiro_capabilities(KiroEngine.V2, requested, runner=runner)
        assert exc_info.value.capability == "--agent-engine=v2"
    else:
        # Selector accepts v2 and no other flag is required → probe succeeds.
        capabilities = probe_kiro_capabilities(KiroEngine.V2, requested, runner=runner)
        assert capabilities.supports_agent_engine(KiroEngine.V2)
