"""Bug condition exploration test for #1074: premature ``header_not_found``.

Property 1 (Bug Condition) — a slow-but-valid TUI header must be observed before
refusing. This test encodes the EXPECTED (post-fix) behavior and is written to
FAIL on the unfixed code, where ``_verify_agent_identity`` reads a fixed
``IDENTITY_HEADER_READS`` (5) times, ~2s total, and refuses with an empty
``observed`` when the valid header renders only after that window.

isBugCondition: the requested agent IS loaded by kiro-cli and a valid, full,
single-agent header eventually renders (``<agent> · <model> · ◔ N%``), but only
after the current poll window — so ``observedHeadersDuringPoll`` is empty at the
decision point and the session is wrongly refused as ``header_not_found``.

Scoped PBT approach: because the bug is a deterministic timing/read-count defect,
the property is scoped to concrete delayed-header cases driven by a scripted
``get_history`` whose header-less reads exceed the current window. This keeps the
counterexample reproducible.

Expected outcomes:
- UNFIXED code: these tests FAIL — ``KiroAgentIdentityError`` of kind
  ``header_not_found`` is raised with an empty observation (confirms the bug).
- FIXED code (task 3.1): these same tests PASS — identity confirmed, no error,
  and no keys sent to the pane before confirmation.

Validates: Requirements 1.1, 1.4, 2.1, 2.3
"""

from unittest.mock import patch

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.kiro_cli import (
    IDENTITY_HEADER_READS,
    KiroCliProvider,
)
from cli_agent_orchestrator.providers.kiro_identity import KiroAgentIdentityError

_LAUNCH_COMMAND = "kiro-cli chat --agent-engine v2 --trust-all-tools --agent {agent}"


def _launch_screen(header: str) -> str:
    """A screen in the layout measured on kiro-cli 2.32.0 after an ``--agent`` launch.

    The fallback warning and the echoed command both name the requested agent; only
    the ``<agent> · <model> · ◔ N%`` header row at column 0 says which agent is
    actually running. An empty ``header`` is a screen whose header has not rendered
    yet (the frames during a slow startup).
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

    Non-``visible_only`` reads (the consent-dialog check) always get a header-less
    screen; only the identity check reads ``visible_only=True``.
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


class TestSlowHeaderExploration:
    """#1074 Property 1: a valid header that renders after the current ~2s poll
    window must be observed (not refused prematurely).

    EXPECTED on unfixed code: FAIL — the fixed count of reads elapses with an
    empty observation and ``require_header_identity`` raises ``header_not_found``.
    """

    @pytest.fixture
    def launch(self, monkeypatch):
        # Zero the inter-read sleep so the scripted reads run without real delay
        # (the bug is the read COUNT / window, not wall-clock time in the test).
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

    @staticmethod
    def _identity_reads(backend):
        return [
            c for c in backend.get_history.call_args_list if c.kwargs.get("visible_only") is True
        ]

    @pytest.mark.asyncio
    async def test_developer_header_after_the_window_is_confirmed(self, launch):
        """Case 1 — delayed-header refusal.

        ``get_history`` returns no header for the first ``IDENTITY_HEADER_READS``
        (5) reads, then a valid ``developer · auto · ◔ N%`` header. The requested
        agent IS loaded and a valid, full, single-agent header eventually renders,
        but only after the current window — isBugCondition holds.

        EXPECTED BEHAVIOR (Property 1): identity confirmed, no error, no keys sent
        before confirmation. On unfixed code this FAILS with ``header_not_found``
        (empty observation at the decision point) — proving the premature refusal.
        """
        launch.get_history.side_effect = _delayed_header_reads(
            "developer · auto · ◔ 11% · auth ok", blank_reads=IDENTITY_HEADER_READS
        )
        provider = KiroCliProvider("test1234", "test-session", "window-0", "developer")

        assert await provider.initialize() is True
        assert provider.identity_verified is True
        # No keystrokes to the pane beyond the single launch command before the
        # header confirmed the agent.
        launch.send_keys.assert_called_once_with(
            "test-session", "window-0", _LAUNCH_COMMAND.format(agent="developer")
        )
        launch.send_special_key.assert_not_called()

    @pytest.mark.asyncio
    async def test_code_supervisor_header_after_slow_startup_is_confirmed(self, launch):
        """Case 2 — slow-host supervisor (the reported agent).

        Same shape with ``code_supervisor``; the valid header
        ``code_supervisor · auto · ◔ 2% · ...`` appears only after ~2.5s of
        simulated reads (one read past the 5-read window). isBugCondition holds.

        EXPECTED: confirmed. On unfixed code this FAILS with ``header_not_found``.
        """
        launch.get_history.side_effect = _delayed_header_reads(
            "code_supervisor · auto · ◔ 2% · auth ok", blank_reads=IDENTITY_HEADER_READS
        )
        provider = KiroCliProvider(
            "test5678", "test-session", "window-0", "code_supervisor"
        )

        assert await provider.initialize() is True
        assert provider.identity_verified is True
        launch.send_special_key.assert_not_called()

    @pytest.mark.asyncio
    async def test_header_one_frame_late_past_the_window_is_confirmed(self, launch):
        """Case 3 — header one frame late.

        No header on the first two reads, then a valid header from the third read
        onward — but the window is scoped so the header lands only just past the
        fifth read (``blank_reads`` = ``IDENTITY_HEADER_READS``), simulating the
        clock pushing the fifth read past the window. isBugCondition holds.

        EXPECTED: confirmed. On unfixed code this FAILS with ``header_not_found``.
        """
        launch.get_history.side_effect = _delayed_header_reads(
            "developer · auto · ◔ 3% · auth ok", blank_reads=IDENTITY_HEADER_READS
        )
        provider = KiroCliProvider("test9012", "test-session", "window-0", "developer")

        assert await provider.initialize() is True
        assert provider.identity_verified is True
        # The valid header was eventually observed — more reads than the old window.
        assert len(self._identity_reads(launch)) > IDENTITY_HEADER_READS
        launch.send_special_key.assert_not_called()
