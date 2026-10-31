"""Unit tests for the time-bounded identity poll window (#1074, task 3.4).

These focused unit tests target ``KiroCliProvider._verify_agent_identity`` in
``src/cli_agent_orchestrator/providers/kiro_cli.py`` directly (not the full
``initialize()`` flow) and pin the contract of the time-bounded poll introduced
in task 3.1:

- a valid header that first appears only after the old 5-read window is still
  observed and confirmed;
- a header present on the first read still breaks immediately (fast path, the
  read count is unchanged for that case — exactly one read);
- a header that never renders still refuses with ``header_not_found`` after the
  budget-bounded window;
- the window length is bounded by the remaining ``provider_init_timeout``
  (``remaining_budget``) — never longer than the budget allows — and never
  shorter than the previous ~2 s floor (``IDENTITY_HEADER_MIN_WINDOW_S``);
- no keys are ever sent to the pane during identity verification, whatever the
  outcome (asserts on ``send_keys`` / ``send_special_key``).

The source implementation is NOT modified by these tests. The inter-read sleep
interval ``IDENTITY_HEADER_READ_INTERVAL_S`` is patched to 0 so the scripted
reads run without real wall-clock delay; the implementation falls back to a 0.5 s
divisor for the read-count math in that case, so the read count still scales with
the window exactly as it does in production.

Validates: Requirements 2.1, 2.3, 2.4, 3.6
"""

from __future__ import annotations

import math
from typing import List, Optional
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.providers.kiro_cli import (
    IDENTITY_HEADER_MIN_WINDOW_S,
    IDENTITY_HEADER_READS,
    KiroCliProvider,
)
from cli_agent_orchestrator.providers.kiro_identity import KiroAgentIdentityError

# The 0.5 s fallback the implementation uses to turn the window into a read count
# when IDENTITY_HEADER_READ_INTERVAL_S is 0 (patched to 0 here to drop the sleep).
_COUNT_INTERVAL = 0.5


def _expected_reads(remaining_budget: Optional[float]) -> int:
    """Mirror the implementation's read-count derivation for a given budget.

    window = max(floor, budget);  reads = max(IDENTITY_HEADER_READS, 1 + ceil(window / 0.5)).
    """
    budget = IDENTITY_HEADER_MIN_WINDOW_S if remaining_budget is None else float(remaining_budget)
    window = max(IDENTITY_HEADER_MIN_WINDOW_S, budget)
    return max(IDENTITY_HEADER_READS, 1 + math.ceil(window / _COUNT_INTERVAL))


def _header_screen(header: str) -> str:
    """A minimal settled screen; an empty ``header`` has not rendered its row yet."""
    rows = [
        "% kiro-cli chat --agent-engine v2 --trust-all-tools --agent developer",
    ]
    if header:
        rows.append(f"{header}                         ~/project")
    rows.append("\u203a  ask a question or describe a task \u21b5")
    return "\n".join(rows) + "\n"


def _reads_then_header(header: str, blank_reads: int):
    """``get_history`` side effect: header-less viewport for the first
    ``blank_reads`` identity reads, then a screen carrying ``header`` thereafter.

    Only ``visible_only`` reads are the identity poll; any other read (e.g. a
    consent-dialog check) gets a header-less screen and is not counted.
    """
    state = {"n": 0}

    def _side_effect(*args, **kwargs):
        if not kwargs.get("visible_only"):
            return _header_screen("")
        state["n"] += 1
        return _header_screen(header) if state["n"] > blank_reads else _header_screen("")

    return _side_effect


def _identity_reads(backend) -> List:
    return [c for c in backend.get_history.call_args_list if c.kwargs.get("visible_only") is True]


def _provider(agent: str = "developer") -> KiroCliProvider:
    return KiroCliProvider("uw000001", "uw-session", "uw-window", agent)


@pytest.fixture(autouse=True)
def _no_sleep():
    """Drop the real inter-read sleep so scripted reads run instantly.

    The implementation then uses a 0.5 s fallback divisor for the read-count
    math, so the number of reads still scales with the window.
    """
    with patch("cli_agent_orchestrator.providers.kiro_cli.IDENTITY_HEADER_READ_INTERVAL_S", 0):
        yield


class TestTimeBoundedPollConfirmsDelayedHeader:
    """A valid header appearing after the old 5-read window is still confirmed."""

    @pytest.mark.asyncio
    async def test_confirms_header_that_appears_after_the_old_window(self):
        """Validates: Requirements 2.1, 2.3

        With a budget wider than the old floor, a valid ``developer`` header that
        renders only after the first ``IDENTITY_HEADER_READS`` reads is observed,
        and identity is confirmed — no error, and no keys sent to the pane.
        """
        backend = MagicMock()
        backend.get_history.side_effect = _reads_then_header(
            "developer \u00b7 auto \u00b7 \u25d4 11% \u00b7 auth ok",
            blank_reads=IDENTITY_HEADER_READS,
        )
        provider = _provider("developer")
        with patch("cli_agent_orchestrator.providers.kiro_cli.get_backend", return_value=backend):
            await provider._verify_agent_identity(remaining_budget=10.0)

        assert provider.identity_verified is True
        # The header was only observable after the old window, so the poll read
        # more than IDENTITY_HEADER_READS times before breaking.
        assert len(_identity_reads(backend)) > IDENTITY_HEADER_READS
        backend.send_keys.assert_not_called()
        backend.send_special_key.assert_not_called()


class TestFastPathUnchanged:
    """A header on the first read breaks immediately with exactly one read."""

    @pytest.mark.asyncio
    async def test_header_on_first_read_breaks_with_a_single_read(self):
        """Validates: Requirements 3.6

        The fast-startup path is not slowed: a header present on the very first
        read confirms immediately after exactly one read, even with a large
        budget that would otherwise permit many reads.
        """
        backend = MagicMock()
        backend.get_history.return_value = _header_screen(
            "developer \u00b7 auto \u00b7 \u25d4 4% \u00b7 Midway: 10h 20m"
        )
        provider = _provider("developer")
        with patch("cli_agent_orchestrator.providers.kiro_cli.get_backend", return_value=backend):
            await provider._verify_agent_identity(remaining_budget=30.0)

        assert provider.identity_verified is True
        assert len(_identity_reads(backend)) == 1
        backend.send_keys.assert_not_called()
        backend.send_special_key.assert_not_called()


class TestGenuineAbsenceStillFailsClosed:
    """A header that never renders still refuses with ``header_not_found``."""

    @pytest.mark.asyncio
    async def test_never_rendered_header_refuses_after_bounded_window(self):
        """Validates: Requirements 2.4

        When no header ever appears, the poll exhausts its budget-bounded reads
        and fails closed with ``header_not_found``, sending no keys to the pane.
        The read count equals exactly what the budget allows.
        """
        backend = MagicMock()
        backend.get_history.return_value = _header_screen("")  # no header row, ever
        provider = _provider("developer")
        with (
            patch("cli_agent_orchestrator.providers.kiro_cli.get_backend", return_value=backend),
            pytest.raises(KiroAgentIdentityError) as excinfo,
        ):
            await provider._verify_agent_identity(remaining_budget=3.0)

        assert excinfo.value.kind == "header_not_found"
        assert provider.identity_verified is False
        assert len(_identity_reads(backend)) == _expected_reads(3.0)
        backend.send_keys.assert_not_called()
        backend.send_special_key.assert_not_called()


class TestWindowIsBudgetBounded:
    """The window is floored at ~2 s and never longer than the remaining budget."""

    @pytest.mark.asyncio
    async def test_small_budget_applies_the_two_second_floor(self):
        """Validates: Requirements 2.3, 2.4

        A ``remaining_budget`` below the ~2 s floor does not shorten the window:
        the read count stays at the ``IDENTITY_HEADER_READS`` floor (the old
        ~2 s wait), never fewer.
        """
        backend = MagicMock()
        backend.get_history.return_value = _header_screen("")
        provider = _provider("developer")
        with (
            patch("cli_agent_orchestrator.providers.kiro_cli.get_backend", return_value=backend),
            pytest.raises(KiroAgentIdentityError),
        ):
            await provider._verify_agent_identity(remaining_budget=0.3)

        reads = len(_identity_reads(backend))
        assert reads == IDENTITY_HEADER_READS
        assert reads == _expected_reads(0.3)

    @pytest.mark.asyncio
    async def test_direct_call_without_budget_uses_only_the_floor(self):
        """Validates: Requirements 2.3

        A direct call (``remaining_budget=None``) reproduces the old fixed wait:
        the read count is exactly the ``IDENTITY_HEADER_READS`` floor.
        """
        backend = MagicMock()
        backend.get_history.return_value = _header_screen("")
        provider = _provider("developer")
        with (
            patch("cli_agent_orchestrator.providers.kiro_cli.get_backend", return_value=backend),
            pytest.raises(KiroAgentIdentityError),
        ):
            await provider._verify_agent_identity()

        assert len(_identity_reads(backend)) == IDENTITY_HEADER_READS

    @pytest.mark.asyncio
    async def test_large_budget_allows_more_reads_than_the_floor(self):
        """Validates: Requirements 2.1, 2.3

        A budget well above the floor stretches the window so the poll performs
        more reads than ``IDENTITY_HEADER_READS`` — the extra patience a slow
        header needs — matching the implementation's derived count exactly.
        """
        backend = MagicMock()
        backend.get_history.return_value = _header_screen("")
        provider = _provider("developer")
        with (
            patch("cli_agent_orchestrator.providers.kiro_cli.get_backend", return_value=backend),
            pytest.raises(KiroAgentIdentityError),
        ):
            await provider._verify_agent_identity(remaining_budget=10.0)

        reads = len(_identity_reads(backend))
        assert reads > IDENTITY_HEADER_READS
        assert reads == _expected_reads(10.0)

    @pytest.mark.asyncio
    async def test_reads_never_exceed_what_the_budget_allows(self):
        """Validates: Requirements 2.3, 2.4

        The window never runs longer than the remaining budget permits: across a
        range of budgets, the actual read count equals the budget-derived bound
        and never more.
        """
        for budget in (0.5, 2.0, 4.0, 7.5, 12.0):
            backend = MagicMock()
            backend.get_history.return_value = _header_screen("")
            provider = _provider("developer")
            with (
                patch(
                    "cli_agent_orchestrator.providers.kiro_cli.get_backend",
                    return_value=backend,
                ),
                pytest.raises(KiroAgentIdentityError),
            ):
                await provider._verify_agent_identity(remaining_budget=budget)

            bound = _expected_reads(budget)
            reads = len(_identity_reads(backend))
            assert reads == bound, f"budget={budget}: expected {bound} reads, got {reads}"
            assert reads <= bound


class TestNoKeysSentRegardlessOfOutcome:
    """No keys reach the pane during identity verification, confirmed or refused."""

    @pytest.mark.asyncio
    async def test_no_keys_sent_on_confirmation(self):
        """Validates: Requirements 3.6"""
        backend = MagicMock()
        backend.get_history.return_value = _header_screen(
            "developer \u00b7 auto \u00b7 \u25d4 4% \u00b7 Midway: 10h 20m"
        )
        provider = _provider("developer")
        with patch("cli_agent_orchestrator.providers.kiro_cli.get_backend", return_value=backend):
            await provider._verify_agent_identity(remaining_budget=5.0)

        assert provider.identity_verified is True
        backend.send_keys.assert_not_called()
        backend.send_special_key.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_keys_sent_on_mismatch_refusal(self):
        """Validates: Requirements 2.4, 3.6

        A header naming the wrong (fallback) agent refuses with
        ``identity_mismatch`` and still sends nothing to the pane.
        """
        backend = MagicMock()
        backend.get_history.return_value = _header_screen(
            "kiro_default \u00b7 auto \u00b7 \u25d4 4% \u00b7 Midway: 10h 20m"
        )
        provider = _provider("developer")
        with (
            patch("cli_agent_orchestrator.providers.kiro_cli.get_backend", return_value=backend),
            pytest.raises(KiroAgentIdentityError) as excinfo,
        ):
            await provider._verify_agent_identity(remaining_budget=5.0)

        assert excinfo.value.kind == "identity_mismatch"
        assert provider.identity_verified is False
        backend.send_keys.assert_not_called()
        backend.send_special_key.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_keys_sent_on_header_not_found_refusal(self):
        """Validates: Requirements 2.4, 3.6"""
        backend = MagicMock()
        backend.get_history.return_value = _header_screen("")
        provider = _provider("developer")
        with (
            patch("cli_agent_orchestrator.providers.kiro_cli.get_backend", return_value=backend),
            pytest.raises(KiroAgentIdentityError) as excinfo,
        ):
            await provider._verify_agent_identity(remaining_budget=5.0)

        assert excinfo.value.kind == "header_not_found"
        backend.send_keys.assert_not_called()
        backend.send_special_key.assert_not_called()
