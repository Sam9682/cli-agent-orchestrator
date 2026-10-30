"""Integration coverage for the engine-flag detection fix launch flow.

These exercise the full launch path (``terminal_service.create_terminal`` and
the ``POST /sessions`` API seam above it) rather than the probe/command units in
``test/providers/test_kiro_capabilities.py``. They confirm that the probed
``KiroCapabilities.agent_engines`` is threaded through
``provider_manager.create_provider(advertises_agent_engine=...)`` to the
``KiroCliProvider`` so a legacy wrapper (kiro-cli 1.28.3, which advertises no
``--agent-engine`` option) launches ``kiro-cli chat`` WITHOUT the engine flag,
while a modern wrapper (2.13.0) still launches ``--agent-engine v2``.

The real probe is reused against the committed 1.28.3 / 2.13.0 help fixtures
(no real subprocess is spawned — a bounded ``runner`` replays the fixtures), so
the ``agent_engines`` the launch path keys on is produced by the production
parser, not hand-stubbed. See the design's "Integration Tests" bullets for
``kiro-agent-engine-flag-detection-fix`` (Requirements 2.3, 3.3).
"""

import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from cli_agent_orchestrator.models.agent_profile import AgentProfile
from cli_agent_orchestrator.models.kiro_engine import KiroEngine
from cli_agent_orchestrator.providers.kiro_capabilities import (
    KiroCapabilityError,
    KiroPhase0KASError,
    build_kiro_command,
    probe_kiro_capabilities,
)
from cli_agent_orchestrator.providers.kiro_cli import KiroCliProvider
from cli_agent_orchestrator.services.terminal_service import create_terminal

_MODULE = "cli_agent_orchestrator.services.terminal_service"

pytestmark = pytest.mark.usefixtures("isolated_memory_db")

_FIXTURES_DIR = Path(__file__).resolve().parents[1] / "providers" / "fixtures"
_LEGACY_ROOT_HELP = (_FIXTURES_DIR / "kiro_cli_1_28_3_root_help.txt").read_text(encoding="utf-8")
_LEGACY_CHAT_HELP = (_FIXTURES_DIR / "kiro_cli_1_28_3_chat_help.txt").read_text(encoding="utf-8")
_MODERN_ROOT_HELP = (_FIXTURES_DIR / "kiro_cli_2_13_0_root_help.txt").read_text(encoding="utf-8")
_MODERN_CHAT_HELP = (_FIXTURES_DIR / "kiro_cli_2_13_0_chat_help.txt").read_text(encoding="utf-8")


def _fixture_probe(root_help: str, chat_help: str, version: str):
    """A launch-path probe that replays one wrapper fixture via the real parser.

    Signature matches ``create_terminal``'s ``kiro_capability_probe`` seam
    (``(engine, requested) -> KiroCapabilities``) and the module-level
    ``probe_kiro_capabilities`` the API path reaches, so the same double serves
    both the direct-call and ``POST /sessions`` tests. No real ``kiro-cli`` is
    spawned: the bounded ``runner`` returns the committed help text.
    """

    def runner(command, **_kwargs):
        if command == ["kiro-cli", "--help"]:
            output = root_help
        elif command == ["kiro-cli", "chat", "--help"]:
            output = chat_help
        else:
            output = f"kiro-cli version {version}"
        return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

    def probe(engine, requested):
        return probe_kiro_capabilities(engine, requested, runner=runner)

    return probe


def _legacy_probe():
    return _fixture_probe(_LEGACY_ROOT_HELP, _LEGACY_CHAT_HELP, "1.28.3")


def _modern_probe():
    return _fixture_probe(_MODERN_ROOT_HELP, _MODERN_CHAT_HELP, "2.13.0")


@pytest.fixture
def installed_supervisor(tmp_path, monkeypatch):
    """``code_supervisor`` installed for Kiro: a v2 launch needs an agent kiro-cli loads."""
    from cli_agent_orchestrator.services import install_service

    agents = tmp_path / "kiro-agents"
    agents.mkdir()
    (agents / "code_supervisor.json").write_text('{"name": "code_supervisor"}', encoding="utf-8")
    (agents / "developer.json").write_text('{"name": "developer"}', encoding="utf-8")
    monkeypatch.setattr(install_service, "KIRO_AGENTS_DIR", agents)


def _healthy_kiro_provider():
    """A provider double that reports a healthy Kiro launch (#1074)."""
    provider = MagicMock()
    provider.initialize = AsyncMock(return_value=True)
    provider.identity_verified = True
    provider.shell_baseline = None
    provider.runtime_variant = None
    return provider


# ---------------------------------------------------------------------------
# POST /sessions against a stubbed 1.28.3 probe (Requirement 2.3)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("installed_supervisor")
async def test_post_sessions_against_legacy_1_28_3_probe_creates_terminal_not_400(monkeypatch):
    """A code_supervisor launch against kiro-cli 1.28.3 succeeds instead of HTTP 400.

    Before the fix the v2 path hard-required ``--agent-engine``; the 1.28.3
    wrapper advertises none, so the probe raised ``KiroCapabilityError`` and the
    API mapped it to 400. The relaxed probe accepts the legacy wrapper, the
    launch allocates a terminal, and the response is a successful session
    creation.

    Validates: Requirement 2.3
    """
    from fastapi.testclient import TestClient

    from cli_agent_orchestrator.api.main import app
    from cli_agent_orchestrator.plugins import PluginRegistry

    # The API path reaches the module-level probe (no kiro_capability_probe
    # seam); override the autouse service-test stub with the legacy fixture.
    monkeypatch.setattr(f"{_MODULE}.probe_kiro_capabilities", _legacy_probe())

    provider = _healthy_kiro_provider()
    app.state.plugin_registry = PluginRegistry()

    with (
        patch(
            f"{_MODULE}.agent_profiles.load_agent_profile",
            return_value=AgentProfile(name="code_supervisor", description="Supervisor"),
        ),
        patch(f"{_MODULE}.get_backend") as backend,
        patch(f"{_MODULE}.db_create_terminal"),
        patch(f"{_MODULE}.mark_terminal_identity_verified", return_value=True),
        patch(f"{_MODULE}.delete_terminals_by_session"),
        patch(f"{_MODULE}.fifo_manager"),
        patch(f"{_MODULE}.provider_manager") as providers,
        patch(f"{_MODULE}.generate_terminal_id", return_value="test1234"),
        patch(f"{_MODULE}.generate_session_name", return_value="session"),
        patch(f"{_MODULE}.generate_window_name", return_value="code_supervisor-window"),
        patch(f"{_MODULE}.get_herdr_inbox_service", return_value=None),
    ):
        backend.return_value.session_exists.return_value = False
        backend.return_value.supports_event_inbox.return_value = True
        providers.create_provider.return_value = provider

        response = TestClient(app, headers={"Host": "localhost"}).post(
            "/sessions",
            params={"provider": "kiro_cli", "agent_profile": "code_supervisor"},
        )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["id"] == "test1234"
    assert body["agent_profile"] == "code_supervisor"
    assert body["engine"] == KiroEngine.V2.value
    # The launch threaded the legacy wrapper's empty selector set into the
    # provider: no --agent-engine advertised.
    assert providers.create_provider.call_args.kwargs["advertises_agent_engine"] is False
    provider.initialize.assert_awaited_once()


# ---------------------------------------------------------------------------
# Context switching: legacy vs modern wrappers, no cross-contamination
# ---------------------------------------------------------------------------


def _capturing_provider_factory(built_commands):
    """A create_provider side_effect building a REAL KiroCliProvider per launch.

    Each provider's ``initialize`` is stubbed to record the exact argv
    ``build_kiro_command`` produces for that launch, using the provider's own
    threaded ``_advertises_agent_engine`` — the same value the production
    ``initialize`` passes. That is the command the pane would receive, so a
    recorded ``--agent-engine`` leaking across launches is the cross-
    contamination this test guards against.
    """

    def factory(
        provider_type,
        terminal_id,
        tmux_session,
        tmux_window,
        agent_profile=None,
        allowed_tools=None,
        *,
        advertises_agent_engine=True,
        engine=None,
        model=None,
        **_kwargs,
    ):
        real = KiroCliProvider(
            terminal_id,
            tmux_session,
            tmux_window,
            agent_profile,
            allowed_tools,
            engine=engine,
            model=model,
            advertises_agent_engine=advertises_agent_engine,
        )

        async def _record():
            built_commands.append(
                build_kiro_command(
                    real._engine,
                    real._agent_profile,
                    model=real._get_profile_model(),
                    yolo=True,
                    advertises_agent_engine=real._advertises_agent_engine,
                )
            )
            return True

        real.initialize = _record  # type: ignore[method-assign]
        real.identity_verified = True
        real.shell_baseline = None
        return real

    return factory


@pytest.mark.asyncio
@pytest.mark.usefixtures("installed_supervisor")
async def test_context_switch_legacy_then_modern_builds_the_right_command_each(monkeypatch):
    """Back-to-back legacy and modern launches each build their own command.

    A legacy launch (empty ``agent_engines``) omits ``--agent-engine``; a modern
    launch that follows it still emits ``--agent-engine v2``. Neither leaks the
    other's engine-flag decision — the fact is per-launch, threaded from each
    launch's own probe through ``create_provider``.

    Validates: Requirement 3.3 (preservation of the modern path)
    """
    built_commands: list[list[str]] = []

    with (
        patch(
            f"{_MODULE}.agent_profiles.load_agent_profile",
            return_value=AgentProfile(name="developer", description="Developer"),
        ),
        patch(f"{_MODULE}.get_backend") as backend,
        patch(f"{_MODULE}.db_create_terminal"),
        patch(f"{_MODULE}.mark_terminal_identity_verified", return_value=True),
        patch(f"{_MODULE}.delete_terminals_by_session"),
        patch(f"{_MODULE}.fifo_manager"),
        patch(f"{_MODULE}.provider_manager") as providers,
        patch(f"{_MODULE}.generate_session_name", side_effect=["s-legacy", "s-modern"]),
        patch(f"{_MODULE}.generate_window_name", return_value="developer-window"),
        patch(f"{_MODULE}.get_herdr_inbox_service", return_value=None),
    ):
        backend.return_value.session_exists.return_value = False
        backend.return_value.supports_event_inbox.return_value = True
        providers.create_provider.side_effect = _capturing_provider_factory(built_commands)

        with patch(f"{_MODULE}.generate_terminal_id", return_value="legacy01"):
            legacy = await create_terminal(
                provider="kiro_cli",
                agent_profile="developer",
                new_session=True,
                kiro_capability_probe=_legacy_probe(),
            )
        with patch(f"{_MODULE}.generate_terminal_id", return_value="modern01"):
            modern = await create_terminal(
                provider="kiro_cli",
                agent_profile="developer",
                new_session=True,
                kiro_capability_probe=_modern_probe(),
            )

    legacy_kwargs, modern_kwargs = providers.create_provider.call_args_list
    assert legacy_kwargs.kwargs["advertises_agent_engine"] is False
    assert modern_kwargs.kwargs["advertises_agent_engine"] is True

    legacy_command, modern_command = built_commands
    assert legacy_command == ["kiro-cli", "chat", "--trust-all-tools", "--agent", "developer"]
    assert "--agent-engine" not in legacy_command
    assert modern_command == [
        "kiro-cli",
        "chat",
        "--agent-engine",
        "v2",
        "--trust-all-tools",
        "--agent",
        "developer",
    ]
    assert legacy.id == "legacy01" and modern.id == "modern01"


@pytest.mark.asyncio
@pytest.mark.usefixtures("installed_supervisor")
async def test_context_switch_modern_then_legacy_does_not_carry_the_flag_back(monkeypatch):
    """Reversing the order proves the modern flag does not persist into a later legacy launch.

    Validates: Requirement 3.3
    """
    built_commands: list[list[str]] = []

    with (
        patch(
            f"{_MODULE}.agent_profiles.load_agent_profile",
            return_value=AgentProfile(name="developer", description="Developer"),
        ),
        patch(f"{_MODULE}.get_backend") as backend,
        patch(f"{_MODULE}.db_create_terminal"),
        patch(f"{_MODULE}.mark_terminal_identity_verified", return_value=True),
        patch(f"{_MODULE}.delete_terminals_by_session"),
        patch(f"{_MODULE}.fifo_manager"),
        patch(f"{_MODULE}.provider_manager") as providers,
        patch(f"{_MODULE}.generate_session_name", side_effect=["s-modern", "s-legacy"]),
        patch(f"{_MODULE}.generate_window_name", return_value="developer-window"),
        patch(f"{_MODULE}.get_herdr_inbox_service", return_value=None),
    ):
        backend.return_value.session_exists.return_value = False
        backend.return_value.supports_event_inbox.return_value = True
        providers.create_provider.side_effect = _capturing_provider_factory(built_commands)

        with patch(f"{_MODULE}.generate_terminal_id", return_value="modern01"):
            await create_terminal(
                provider="kiro_cli",
                agent_profile="developer",
                new_session=True,
                kiro_capability_probe=_modern_probe(),
            )
        with patch(f"{_MODULE}.generate_terminal_id", return_value="legacy01"):
            await create_terminal(
                provider="kiro_cli",
                agent_profile="developer",
                new_session=True,
                kiro_capability_probe=_legacy_probe(),
            )

    modern_command, legacy_command = built_commands
    assert "--agent-engine" in modern_command
    assert "--agent-engine" not in legacy_command
    assert legacy_command == ["kiro-cli", "chat", "--trust-all-tools", "--agent", "developer"]


# ---------------------------------------------------------------------------
# Preserved launch-flow branches: kas rejection and the model-fallback retry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_launch_flow_still_rejects_kas_with_phase0_error():
    """A kas launch is still refused with KiroPhase0KASError before any allocation.

    The engine-flag fix touches only the v2 empty-selector case; the kas branch
    in the launch flow is unchanged.

    Validates: Requirement 3.3
    """
    from cli_agent_orchestrator.providers.kiro_capabilities import KiroCapabilities

    probe = Mock(
        return_value=KiroCapabilities(version="2.13.0", flags=frozenset({"--v3", "--agent"}))
    )
    profile = AgentProfile(
        name="kas-profile",
        description="KAS profile",
        engine=KiroEngine.KAS,
        allowedTools=["fs_read"],
        toolsSettings={"cao-mcp-server": {"enabled": True}},
    )

    with (
        patch(f"{_MODULE}.agent_profiles.load_agent_profile", return_value=profile),
        patch(f"{_MODULE}.get_backend") as backend,
        patch(f"{_MODULE}.db_create_terminal") as db_create,
        patch(f"{_MODULE}.fifo_manager") as fifo,
        patch(f"{_MODULE}.provider_manager") as providers,
    ):
        with pytest.raises(KiroPhase0KASError, match="Cedar"):
            await create_terminal(
                provider="kiro_cli",
                agent_profile="kas-profile",
                new_session=True,
                kiro_capability_probe=probe,
            )

    probe.assert_called_once_with(KiroEngine.KAS, {"profile"})
    backend.return_value.create_session.assert_not_called()
    db_create.assert_not_called()
    fifo.create_reader.assert_not_called()
    providers.create_provider.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.usefixtures("installed_supervisor")
async def test_launch_flow_model_fallback_retry_is_unchanged_for_legacy_wrapper():
    """The model-fallback retry still fires, and the retried legacy launch omits the flag.

    When the first probe raises for the planned model, the launch path retries
    with the profile/base model (``launch_plan.model`` present, ``model`` not
    explicitly supplied). The fix does not alter that retry: here the retry
    probes the legacy wrapper, which succeeds and threads
    ``advertises_agent_engine=False`` to the provider.

    Validates: Requirement 3.3
    """
    from cli_agent_orchestrator.decisions.types import LaunchPlan

    calls: list = []

    def flaky_probe(engine, requested):
        calls.append(requested)
        if "model" in requested:
            # First attempt: the planned model's --model is not advertised.
            raise KiroCapabilityError(
                "unsupported_capability",
                engine,
                "model flag missing",
                capability="--model",
                version="1.28.3",
            )
        # Retry without the model capability: the legacy wrapper is accepted.
        return _legacy_probe()(engine, requested)

    provider = _healthy_kiro_provider()

    with (
        patch(
            f"{_MODULE}.agent_profiles.load_agent_profile",
            return_value=AgentProfile(name="developer", description="Developer"),
        ),
        patch(f"{_MODULE}.get_backend") as backend,
        patch(f"{_MODULE}.db_create_terminal"),
        patch(f"{_MODULE}.mark_terminal_identity_verified", return_value=True),
        patch(f"{_MODULE}.delete_terminals_by_session"),
        patch(f"{_MODULE}.fifo_manager"),
        patch(f"{_MODULE}.provider_manager") as providers,
        patch(f"{_MODULE}.generate_terminal_id", return_value="test1234"),
        patch(f"{_MODULE}.generate_session_name", return_value="session"),
        patch(f"{_MODULE}.generate_window_name", return_value="developer-window"),
        patch(f"{_MODULE}.get_herdr_inbox_service", return_value=None),
    ):
        backend.return_value.session_exists.return_value = False
        backend.return_value.supports_event_inbox.return_value = True
        providers.create_provider.return_value = provider

        terminal = await create_terminal(
            provider="kiro_cli",
            agent_profile="developer",
            new_session=True,
            launch_plan=LaunchPlan(model="planned-model"),
            kiro_capability_probe=flaky_probe,
        )

    # Two probe attempts: the first with the model capability (raised), the
    # retry without it (succeeded).
    assert len(calls) == 2
    assert "model" in calls[0]
    assert "model" not in calls[1]
    assert terminal.id == "test1234"
    # The retried legacy launch still threaded an empty selector set.
    assert providers.create_provider.call_args.kwargs["advertises_agent_engine"] is False
