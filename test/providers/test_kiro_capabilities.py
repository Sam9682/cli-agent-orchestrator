"""Deterministic Phase 0 wrapper-probe and command tests."""

import subprocess
from pathlib import Path

import pytest

from cli_agent_orchestrator.models.kiro_engine import KiroEngine
from cli_agent_orchestrator.providers.kiro_capabilities import (
    KiroCapabilityError,
    _flags_from_help,
    build_kiro_command,
    probe_kiro_capabilities,
    requested_kiro_capabilities,
)

_HELP = (
    "kiro-cli version 2.13.0\n"
    "--agent-engine v2|v1|v3\n--v3\n--agent NAME\n--model MODEL\n"
    "--legacy-ui\n--trust-all-tools\n--require-mcp-startup\n"
)
_FIXTURES_DIR = Path(__file__).parent / "fixtures"
_RELEASED_ROOT_HELP = (_FIXTURES_DIR / "kiro_cli_2_13_0_root_help.txt").read_text(encoding="utf-8")
_RELEASED_CHAT_HELP = (_FIXTURES_DIR / "kiro_cli_2_13_0_chat_help.txt").read_text(encoding="utf-8")
_LEGACY_ROOT_HELP = (_FIXTURES_DIR / "kiro_cli_1_28_3_root_help.txt").read_text(encoding="utf-8")
_LEGACY_CHAT_HELP = (_FIXTURES_DIR / "kiro_cli_1_28_3_chat_help.txt").read_text(encoding="utf-8")


def _help_runner(root_help, chat_help, version):
    """Build a bounded root/chat/version probe runner for one wrapper fixture."""

    def _run(command, **_kwargs):
        if command == ["kiro-cli", "--help"]:
            output = root_help
        elif command == ["kiro-cli", "chat", "--help"]:
            output = chat_help
        else:
            output = f"kiro-cli version {version}"
        return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

    return _run


def _runner(command, **_kwargs):
    output = "kiro-cli version 2.13.0" if command[-1] == "--version" else _HELP
    return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")


def test_probe_accepts_v2_and_requested_wrapper_features():
    result = probe_kiro_capabilities(
        KiroEngine.V2, {"profile", "model", "ui", "trust"}, runner=_runner
    )

    assert result.version == "2.13.0"
    assert result.supports("--agent-engine")
    assert result.agent_engines == frozenset({"v1", "v2", "v3"})


def test_probe_accepts_released_wrapper_help_before_explicit_v2_construction():
    """The released wrapper splits root commands from chat engine capabilities."""
    calls = []

    def released_wrapper_runner(command, **kwargs):
        calls.append((command, kwargs))
        if command == ["kiro-cli", "--help"]:
            output = _RELEASED_ROOT_HELP
        elif command == ["kiro-cli", "chat", "--help"]:
            output = _RELEASED_CHAT_HELP
        else:
            output = "kiro-cli version 2.13.0"
        return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

    capabilities = probe_kiro_capabilities(
        KiroEngine.V2,
        {"profile", "model", "ui", "trust", "mcp_startup"},
        timeout=1.0,
        chat_help_timeout=7.5,
        runner=released_wrapper_runner,
    )

    assert capabilities.version == "2.13.0"
    assert capabilities.agent_engines == frozenset({"v1", "v2", "v3"})
    assert build_kiro_command(KiroEngine.V2, "developer") == [
        "kiro-cli",
        "chat",
        "--agent-engine",
        "v2",
        "--agent",
        "developer",
    ]
    assert calls == [
        (
            ["kiro-cli", "--help"],
            {"capture_output": True, "text": True, "timeout": 1.0},
        ),
        (
            ["kiro-cli", "chat", "--help"],
            {
                "capture_output": True,
                "text": True,
                "timeout": 7.5,
                "stdin": subprocess.DEVNULL,
            },
        ),
        (
            ["kiro-cli", "--version"],
            {"capture_output": True, "text": True, "timeout": 1.0},
        ),
    ]


def test_probe_rejects_required_flag_mentioned_only_in_prose():
    def prose_only_flag_runner(command, **_kwargs):
        output = (
            "--agent-engine v2\n--agent NAME\n--legacy-ui\n"
            "Use --trust-all-tools to bypass confirmation.\n"
            if command == ["kiro-cli", "chat", "--help"]
            else "kiro-cli version 2.13.0"
        )
        return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

    with pytest.raises(KiroCapabilityError, match="--trust-all-tools") as exc_info:
        probe_kiro_capabilities(
            KiroEngine.V2,
            {"profile", "ui", "trust"},
            runner=prose_only_flag_runner,
        )

    assert exc_info.value.capability == "--trust-all-tools"


@pytest.mark.parametrize(
    "flag",
    [
        "--agent-engine",
        "--v3",
        "--agent",
        "--model",
        "--legacy-ui",
        "--trust-all-tools",
        "--require-mcp-startup",
    ],
)
def test_flags_from_help_rejects_leading_prose_for_every_requested_capability(flag):
    """Capability tokens at the start of prose are not option declarations."""
    assert _flags_from_help(f"{flag} bypasses confirmation when enabled") == frozenset()


def test_flags_from_help_rejects_short_bare_agent_engine_prose():
    assert _flags_from_help("--agent-engine bypasses") == frozenset()


@pytest.mark.parametrize(
    "flag",
    [
        "--v3",
        "--legacy-ui",
        "--trust-all-tools",
        "--require-mcp-startup",
    ],
)
@pytest.mark.parametrize("suffix", ["...", " VALUE"])
def test_flags_from_help_rejects_incompatible_bare_boolean_declarations(flag, suffix):
    assert _flags_from_help(f"{flag}{suffix}") == frozenset()


@pytest.mark.parametrize(
    "flag",
    [
        "--agent-engine",
        "--v3",
        "--agent",
        "--model",
        "--legacy-ui",
        "--trust-all-tools",
        "--require-mcp-startup",
    ],
)
def test_flags_from_help_rejects_ellipsis_suffixed_requested_candidates(flag):
    assert _flags_from_help(f"{flag}...") == frozenset()


def test_flags_from_help_accepts_formal_short_long_metavariable_alias_and_variadic_forms():
    flags = _flags_from_help(
        "\n".join(
            [
                "--agent-engine <ENGINE>",
                "--v3",
                "--agent <AGENT>",
                "--model MODEL",
                "--legacy-ui, --legacy",
                "-a, --trust-all-tools",
                "-v, --verbose...",
                "--require-mcp-startup",
            ]
        )
    )

    assert flags == frozenset(
        {
            "--agent-engine",
            "--v3",
            "--agent",
            "--model",
            "--legacy-ui",
            "--trust-all-tools",
            "--require-mcp-startup",
        }
    )


def test_flags_from_help_does_not_advertise_requested_capabilities_from_verbose_variadic_form():
    assert _flags_from_help("-v, --verbose...") == frozenset()


def test_probe_rejects_root_prose_and_chat_prose_only_agent_engine_values():
    def prose_only_engine_runner(command, **_kwargs):
        if command == ["kiro-cli", "--help"]:
            output = "Use --agent-engine <ENGINE> with possible values: v2, v1, v3.\n"
        elif command == ["kiro-cli", "chat", "--help"]:
            output = (
                "--agent-engine <ENGINE>\n"
                "Agent engine configuration is documented elsewhere.\n"
                "The supported possible values: v2, v1, v3.\n"
                "--agent NAME\n--legacy-ui\n"
            )
        else:
            output = "kiro-cli version 2.13.0"
        return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

    with pytest.raises(KiroCapabilityError, match="explicit 'v2'") as exc_info:
        probe_kiro_capabilities(
            KiroEngine.V2,
            {"profile", "ui"},
            runner=prose_only_engine_runner,
        )

    assert exc_info.value.capability == "--agent-engine=v2"


@pytest.mark.parametrize(
    "declaration",
    [
        "v2",
        "<v1|v2|v3>",
        "[v1|v2|v3]",
        "{v1,v2,v3}",
    ],
)
def test_probe_accepts_explicit_v2_agent_engine_declarations(declaration):
    def v2_declaration_runner(command, **_kwargs):
        output = (
            f"--agent-engine {declaration}\n--agent NAME\n--legacy-ui\n"
            if command[-1] == "--help"
            else "kiro-cli 2.13.0"
        )
        return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

    result = probe_kiro_capabilities(
        KiroEngine.V2,
        {"profile", "ui"},
        runner=v2_declaration_runner,
    )

    assert result.supports_agent_engine(KiroEngine.V2)


def test_probe_rejects_agent_engine_declaration_that_excludes_v2():
    def v1_v3_only_runner(command, **_kwargs):
        output = (
            "--agent-engine v1|v3\n--agent NAME\n--legacy-ui\n"
            if command[-1] == "--help"
            else "kiro-cli 2.13.0"
        )
        return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

    with pytest.raises(KiroCapabilityError, match="accept 'v2'") as exc_info:
        probe_kiro_capabilities(
            KiroEngine.V2,
            {"profile", "ui"},
            runner=v1_v3_only_runner,
        )

    assert exc_info.value.kind == "unsupported_capability"
    assert exc_info.value.capability == "--agent-engine=v2"


@pytest.mark.parametrize(
    "declaration",
    [
        "--agent-engine",
        "--agent-engine <v1|v2",
        "--agent-engine v1|v2/",
    ],
)
def test_probe_rejects_malformed_agent_engine_declarations(declaration):
    def malformed_declaration_runner(command, **_kwargs):
        output = (
            f"{declaration}\n--agent NAME\n--legacy-ui\n"
            if command[-1] == "--help"
            else "kiro-cli 2.13.0"
        )
        return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

    with pytest.raises(KiroCapabilityError, match="explicit 'v2'") as exc_info:
        probe_kiro_capabilities(
            KiroEngine.V2,
            {"profile", "ui"},
            runner=malformed_declaration_runner,
        )

    assert exc_info.value.kind == "unsupported_capability"
    assert exc_info.value.capability == "--agent-engine=v2"


def test_probe_uses_bounded_root_chat_and_version_wrapper_contracts():
    commands = []

    def recording_runner(command, **_kwargs):
        commands.append(command)
        return _runner(command)

    probe_kiro_capabilities(KiroEngine.V2, {"profile"}, runner=recording_runner)

    assert commands == [
        ["kiro-cli", "--help"],
        ["kiro-cli", "chat", "--help"],
        ["kiro-cli", "--version"],
    ]


def test_probe_rejects_unsupported_kas_selector_without_fallback():
    def no_v3(command, **_kwargs):
        output = "--agent-engine\n--agent\n" if command[-1] == "--help" else "kiro-cli 2.13.0"
        return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

    with pytest.raises(KiroCapabilityError, match="--v3") as exc_info:
        probe_kiro_capabilities(KiroEngine.KAS, {"profile"}, runner=no_v3)

    assert exc_info.value.kind == "unsupported_capability"
    assert exc_info.value.engine == KiroEngine.KAS


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
def test_probe_reports_distinct_execution_failures(runner, kind):
    with pytest.raises(KiroCapabilityError) as exc_info:
        probe_kiro_capabilities(KiroEngine.V2, set(), runner=runner)
    assert exc_info.value.kind == kind


def test_builds_explicit_v2_and_deterministic_kas_commands():
    assert build_kiro_command(KiroEngine.V2, "developer", model="fixture-model", yolo=True) == [
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
    assert build_kiro_command(KiroEngine.KAS, "developer") == [
        "kiro-cli",
        "--v3",
        "chat",
        "--agent",
        "developer",
    ]


def test_v2_command_never_carries_legacy_ui():
    """--legacy-ui must never appear on a v2 command line.

    kiro-cli errors out on the combination ("Conflicting options: --legacy-ui
    cannot be used with --agent-engine=v2"), and the bare flag silently selects
    the v1 engine, which serves no MCP tools — so an agent launched that way
    starts fine and then cannot assign/handoff/report_outcome. Regression guard
    for a silent 7-day self-learning outage.
    """
    for yolo in (False, True):
        for model in (None, "fixture-model"):
            command = build_kiro_command(KiroEngine.V2, "developer", model=model, yolo=yolo)
            assert "--legacy-ui" not in command
            assert "--agent-engine" in command
            assert command[command.index("--agent-engine") + 1] == "v2"


def test_legacy_ui_is_not_a_requested_capability():
    """--legacy-ui is no longer probed for: there is no legacy retry to verify.

    Requiring it would also reject wrappers that dropped the flag but are
    otherwise fully usable by CAO.
    """
    for yolo in (False, True):
        requested = requested_kiro_capabilities(KiroEngine.V2, model=None, yolo=yolo)
        assert "ui" not in requested


# =====================================================================
# Engine-flag detection fix — deterministic unit coverage (Task 4.1)
#
# These cover the design's "Unit Tests" bullets for
# kiro-agent-engine-flag-detection-fix. They are the deterministic,
# example-based complement to the property-based exploration and
# preservation suites in test_kiro_engine_flag_detection_bug.py and
# test_kiro_engine_flag_detection_preservation.py (not duplicated here).
# =====================================================================


def test_probe_v2_against_legacy_1_28_3_fixtures_succeeds_with_empty_agent_engines():
    """Probe v2 against the 1.28.3 legacy fixtures succeeds and reports no selector.

    The 1.28.3 wrapper declares no ``--agent-engine`` option at all, so it is a
    legacy wrapper the probe must accept with an empty ``agent_engines``.

    Validates: Requirements 2.1, 2.4
    """
    runner = _help_runner(_LEGACY_ROOT_HELP, _LEGACY_CHAT_HELP, "1.28.3")

    capabilities = probe_kiro_capabilities(
        KiroEngine.V2,
        {"profile", "model", "trust"},
        runner=runner,
    )

    assert capabilities.version == "1.28.3"
    assert capabilities.agent_engines == frozenset()
    assert not capabilities.supports("--agent-engine")
    assert capabilities.supports("--agent")
    assert capabilities.supports("--trust-all-tools")
    assert capabilities.supports("--model")


def test_build_command_omits_agent_engine_for_legacy_and_includes_it_for_modern():
    """build_kiro_command drops the engine flag for a legacy wrapper, keeps it for modern.

    Validates: Requirements 2.2, 3.1
    """
    legacy_command = build_kiro_command(
        KiroEngine.V2,
        "developer",
        advertises_agent_engine=False,
    )
    assert legacy_command == ["kiro-cli", "chat", "--agent", "developer"]
    assert "--agent-engine" not in legacy_command

    modern_command = build_kiro_command(
        KiroEngine.V2,
        "developer",
        advertises_agent_engine=True,
    )
    assert modern_command == ["kiro-cli", "chat", "--agent-engine", "v2", "--agent", "developer"]


def test_build_command_legacy_path_never_emits_legacy_ui_but_keeps_requested_flags():
    """Legacy path omits --agent-engine/--legacy-ui while keeping --agent/--trust/--model.

    Validates: Requirements 2.2, 2.4
    """
    command = build_kiro_command(
        KiroEngine.V2,
        "developer",
        model="fixture-model",
        yolo=True,
        advertises_agent_engine=False,
    )

    assert command == [
        "kiro-cli",
        "chat",
        "--trust-all-tools",
        "--model",
        "fixture-model",
        "--agent",
        "developer",
    ]
    assert "--agent-engine" not in command
    assert "--legacy-ui" not in command


def test_probe_rejects_declared_selector_without_v2_even_though_guard_is_scoped():
    """A declared-but-not-v2 selector still raises --agent-engine=v2.

    The relaxed empty-selector path must not weaken the explicit-v2 guard: when
    the wrapper DOES advertise ``--agent-engine`` but omits ``v2`` the probe
    keeps rejecting with the existing capability error.

    Validates: Requirement 3.2
    """
    chat_help = (
        "Usage: kiro-cli-chat chat [OPTIONS] [INPUT]\n\n"
        "Options:\n"
        "      --agent <AGENT>\n          Context profile to use\n"
        "  -a, --trust-all-tools\n          Allows the model to use any tool\n"
        "      --agent-engine v1|v3\n          Agent engine to use\n"
    )
    runner = _help_runner(_RELEASED_ROOT_HELP, chat_help, "2.13.0")

    with pytest.raises(KiroCapabilityError, match="accept 'v2'") as exc_info:
        probe_kiro_capabilities(
            KiroEngine.V2,
            {"profile", "trust"},
            runner=runner,
        )

    assert exc_info.value.kind == "unsupported_capability"
    assert exc_info.value.capability == "--agent-engine=v2"
