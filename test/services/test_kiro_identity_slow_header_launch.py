"""Integration tests for the full Kiro launch flow with a slow-but-valid header (#1074).

Task 3.5. The time-bounded identity poll (task 3.1) threads the leftover
``provider_init_timeout`` from ``initialize()`` into ``_verify_agent_identity``
so a header that renders only after the old ~2 s window is still observed within
the startup budget, while every fail-closed outcome is preserved.

These tests drive the full launch flow end to end rather than ``_verify_agent_identity``
in isolation (that is the unit level, task 3.4):

- ``initialize()`` with a backend double whose header is delayed past the old
  window reaches a confirmed identity and returns ``True`` (no
  ``KiroAgentIdentityError``), and stays within ``provider_init_timeout``.
- ``_schedule_deferred_init`` (mirrors ``test_kiro_agent_identity_launch.py``):
  a delayed-but-valid header no longer surfaces a deferred init failure, while a
  genuinely absent header still surfaces ``KiroAgentIdentityError`` with
  "No task was sent".
- A fallback-screen launch still surfaces the ``identity_mismatch`` /
  ``header_not_found`` failure and sends only the launch command to the pane.

Validates: Requirements 2.1, 2.3, 2.4, 3.1, 3.5
"""

import time
from unittest.mock import AsyncMock, Mock, patch

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.kiro_cli import (
    IDENTITY_HEADER_READS,
    KiroCliProvider,
)
from cli_agent_orchestrator.services import terminal_service

_LAUNCH_COMMAND = "kiro-cli chat --agent-engine v2 --trust-all-tools --agent {agent}"


def _launch_screen(header: str) -> str:
    """A screen in the layout measured on kiro-cli 2.32.0 after an ``--agent`` launch.

    Only the ``<agent> · <model> · ◔ N%`` header row at column 0 says which agent
    is actually running. An empty ``header`` is a screen whose header has not
    rendered yet (the frames during a slow startup).
    """
    rule = "─" * 60
    rows = [
        "bash-3.2$ kiro-cli chat --agent-engine v2 --trust-all-tools --agent developer",
        '● agent "developer" not found, using "kiro_default"',
        rule,
        " Trust All Tools active, confirmations are off · /quit to exit",
        rule,
    ]
    if header:
        rows.append(f"{header}                                  /private/tmp/repo")
    rows += ["", "›  ask a question or describe a task ↵"]
    return "\n".join(rows) + "\n"


def _delayed_header_reads(header: str, blank_reads: int):
    """A ``get_history`` side effect that yields a header-less viewport for the
    first ``blank_reads`` identity reads, then a screen with a valid ``header``
    on every read after that.

    Non-``visible_only`` reads (the consent-dialog check) always get a
    header-less screen; only the identity check reads ``visible_only=True``.
    """
    identity_calls = {"n": 0}

    def _side_effect(*args, **kwargs):
        if not kwargs.get("visible_only"):
            return _launch_screen("")
        identity_calls["n"] += 1
        if identity_calls["n"] <= blank_reads:
            return _launch_screen("")
        return _launch_screen(header)

    return _side_effect


def _never_header_reads():
    """A ``get_history`` side effect whose header never renders — a genuine
    absence that must stay ``header_not_found`` after the extended wait."""

    def _side_effect(*args, **kwargs):
        return _launch_screen("")

    return _side_effect


@pytest.fixture
def launch(monkeypatch):
    """Patch the full ``initialize()`` seam like the slow-header exploration test.

    Zeroing the inter-read sleep drops the real wall-clock delay; the read count
    still scales with the window because the poll falls back to the default
    interval as the divisor when the patched interval is 0.
    """
    monkeypatch.setattr(
        "cli_agent_orchestrator.providers.kiro_cli.IDENTITY_HEADER_READ_INTERVAL_S", 0
    )
    with (
        patch("cli_agent_orchestrator.providers.kiro_cli.get_backend") as backend,
        patch("cli_agent_orchestrator.providers.kiro_cli.wait_for_shell", return_value=True),
        patch("cli_agent_orchestrator.providers.kiro_cli.wait_until_status", return_value=True),
        patch(
            "cli_agent_orchestrator.providers.kiro_cli.load_agent_profile",
            side_effect=FileNotFoundError("no profile"),
        ),
        patch("cli_agent_orchestrator.services.status_monitor.status_monitor") as monitor,
    ):
        monitor.get_status.return_value = TerminalStatus.IDLE
        yield backend.return_value


class TestFullInitializeFlowWithADelayedHeader:
    """The full ``initialize()`` flow (shell wait → launch → readiness → identity
    poll) confirms a header that renders only after the old window, within the
    startup budget (Requirements 2.1, 2.3)."""

    @pytest.mark.asyncio
    async def test_initialize_returns_true_for_a_header_after_the_old_window(self, launch):
        """A valid ``developer`` header that first appears one read past the old
        5-read window: ``initialize()`` returns ``True``, identity is confirmed,
        and only the launch command was sent to the pane."""
        launch.get_history.side_effect = _delayed_header_reads(
            "developer · auto · ◔ 11% · auth ok", blank_reads=IDENTITY_HEADER_READS
        )
        provider = KiroCliProvider("test1234", "cao-session", "window-0", "developer")

        assert await provider.initialize() is True
        assert provider.identity_verified is True
        launch.send_keys.assert_called_once_with(
            "cao-session", "window-0", _LAUNCH_COMMAND.format(agent="developer")
        )
        launch.send_special_key.assert_not_called()

    @pytest.mark.asyncio
    async def test_initialize_stays_within_the_startup_budget(self, launch):
        """Total ``initialize()`` time is within ``provider_init_timeout``: a
        delayed header is waited out inside the budget, not on a second timeout
        stacked on top of it."""
        launch.get_history.side_effect = _delayed_header_reads(
            "code_supervisor · auto · ◔ 2% · auth ok", blank_reads=IDENTITY_HEADER_READS
        )
        provider = KiroCliProvider("test5678", "cao-session", "window-0", "code_supervisor")

        from cli_agent_orchestrator.providers.kiro_cli import get_server_settings

        budget = float(get_server_settings()["provider_init_timeout"])
        started = time.monotonic()
        assert await provider.initialize() is True
        elapsed = time.monotonic() - started

        assert provider.identity_verified is True
        assert elapsed < budget


class TestDeferredLaunchConfirmsADelayedHeader:
    """The deferred path (assign) runs ``initialize()`` then types the task; a
    delayed-but-valid header must confirm and no longer surface a deferred init
    failure (Requirements 2.1, 2.3)."""

    @pytest.mark.asyncio
    async def test_a_delayed_header_surfaces_no_deferred_failure(self, launch):
        surface = AsyncMock()
        dispatch = Mock()
        with (
            patch.object(terminal_service, "dispatch_input", dispatch),
            patch.object(terminal_service, "_surface_deferred_init_failure", surface),
            patch.object(terminal_service, "_record_kiro_identity_verified", AsyncMock()),
            patch.object(terminal_service, "update_terminal_shell_command"),
            patch.object(
                terminal_service, "get_terminal_metadata", return_value={"caller_id": None}
            ),
            # The identity poll is what task 3.5 exercises; the post-dispatch
            # worker-start handshake is unrelated downstream machinery (it has
            # its own tests), so pin it to success here.
            patch.object(
                terminal_service,
                "_confirm_worker_started_or_resubmit",
                AsyncMock(return_value=True),
            ),
        ):
            launch.get_history.side_effect = _delayed_header_reads(
                "developer · auto · ◔ 7% · auth ok", blank_reads=IDENTITY_HEADER_READS
            )
            provider = KiroCliProvider("1e293d67", "cao-session", "developer-abcd", "developer")

            task = terminal_service._schedule_deferred_init(
                provider, "1e293d67", "TASK-CANARY do the work", None, None
            )
            await task

        # The header confirmed the agent, so the deferred task dispatched and no
        # failure was surfaced.
        assert provider.identity_verified is True
        surface.assert_not_awaited()
        dispatch.assert_called_once()
        assert [c.args[2] for c in launch.send_keys.call_args_list] == [
            _LAUNCH_COMMAND.format(agent="developer")
        ]
        launch.send_special_key.assert_not_called()


class TestDeferredLaunchStillFailsClosed:
    """Preservation (Requirements 2.4, 3.1): a genuinely absent header and a
    fallback (wrong-agent) header still surface ``KiroAgentIdentityError`` with
    "No task was sent", and only the launch command reaches the pane."""

    @pytest.mark.asyncio
    async def test_a_header_that_never_renders_still_surfaces_the_failure(self, launch):
        surface = AsyncMock()
        dispatch = Mock()
        with (
            patch.object(terminal_service, "dispatch_input", dispatch),
            patch.object(terminal_service, "_surface_deferred_init_failure", surface),
            patch.object(terminal_service, "_clear_deferred_init_external_owner", AsyncMock()),
            patch.object(terminal_service, "update_terminal_shell_command"),
        ):
            launch.get_history.side_effect = _never_header_reads()
            provider = KiroCliProvider("1e293d67", "cao-session", "developer-abcd", "developer")

            task = terminal_service._schedule_deferred_init(
                provider, "1e293d67", "TASK-CANARY do the work", None, None
            )
            await task

        dispatch.assert_not_called()
        assert [c.args[2] for c in launch.send_keys.call_args_list] == [
            _LAUNCH_COMMAND.format(agent="developer")
        ]
        launch.send_special_key.assert_not_called()
        surface.assert_awaited_once()
        kwargs = surface.await_args.kwargs
        assert kwargs["exception_type"] == "KiroAgentIdentityError"
        assert "No task was sent" in kwargs["message"]

    @pytest.mark.asyncio
    async def test_a_fallback_header_still_surfaces_identity_mismatch(self, launch):
        """A fallback screen (``kiro_default`` header, ``developer`` requested)
        still fails closed and sends only the launch command to the pane."""
        surface = AsyncMock()
        dispatch = Mock()
        fallback = (
            "% kiro-cli chat --agent-engine v2 --trust-all-tools --agent developer\n"
            '● agent "developer" not found, using "kiro_default"\n'
            "kiro_default · auto · ◔ 4% · Midway: 10h 20m\n"
            "›  ask a question or describe a task ↵\n"
        )
        with (
            patch.object(terminal_service, "dispatch_input", dispatch),
            patch.object(terminal_service, "_surface_deferred_init_failure", surface),
            patch.object(terminal_service, "_clear_deferred_init_external_owner", AsyncMock()),
            patch.object(terminal_service, "update_terminal_shell_command"),
        ):
            launch.get_history.return_value = fallback
            provider = KiroCliProvider("1e293d67", "cao-session", "developer-abcd", "developer")

            task = terminal_service._schedule_deferred_init(
                provider, "1e293d67", "TASK-CANARY do the work", None, None
            )
            await task

        dispatch.assert_not_called()
        assert [c.args[2] for c in launch.send_keys.call_args_list] == [
            _LAUNCH_COMMAND.format(agent="developer")
        ]
        launch.send_special_key.assert_not_called()
        surface.assert_awaited_once()
        kwargs = surface.await_args.kwargs
        assert kwargs["exception_type"] == "KiroAgentIdentityError"
        assert "kiro_default" in kwargs["message"] and "No task was sent" in kwargs["message"]
