"""Preservation property tests for the Kiro identity poll fix (#1074, Property 2).

These tests pin the behavior that must NOT change when the premature
``header_not_found`` refusal is fixed by lengthening the identity poll window
(see ``.kiro/specs/kiro-agent-identity-spawn-failure``). The fix is confined to
*how long* ``KiroCliProvider._verify_agent_identity`` polls the rendered screen;
the parser (``_HEADER_ROW``, ``header_agents``) and the decision function
(``require_header_identity``, ``in_plan_mode``, ``identity_readable``,
``is_partial_agent_name``) are deliberately left untouched.

Observation-first methodology (bugfix workflow): every case here is run against
the UNFIXED code and records the actual outcome, so that after the fix the SAME
assertions prove the outcome is unchanged. This file therefore MUST pass on the
unfixed code — that green run is the baseline being preserved.

Three property groups, matching the design's "Preservation Checking":

1. ``require_header_identity`` verdict is a pure function of its inputs. Random
   ``observed`` lists (agent sets, partials, fallback names, plan-mode ``Plan``)
   produce a stable verdict. The fix does not call this function differently, so
   this pins that its inputs' meaning is unchanged.
2. ``_verify_agent_identity`` confirms a header present on the first read with no
   extra reads (fast path), and refuses a header that never renders
   (``header_not_found``). These are the two outcomes already observable on the
   UNFIXED code; the within-budget-delay confirm is validated after the fix
   (task 3.2).
3. ``header_agents`` output is a pure function of the pane text. Random pane
   layouts built from fixture header lines parse to the same agent list before
   and after the fix (parser untouched).

Fixtures anchoring these cases: ``test/providers/fixtures/kiro_cli_*.txt``,
plus ``_FALLBACK_SCREEN`` / ``_HEALTHY_SCREEN`` mirrored from
``test/services/test_kiro_agent_identity_launch.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import List
from unittest.mock import MagicMock, patch

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from cli_agent_orchestrator.providers.kiro_cli import (
    IDENTITY_HEADER_READS,
    KiroCliProvider,
)
from cli_agent_orchestrator.providers.kiro_identity import (
    KIRO_FALLBACK_AGENTS,
    PLAN_MODE_AGENT,
    KiroAgentIdentityError,
    header_agents,
    identity_readable,
    is_partial_agent_name,
    require_header_identity,
    require_kiro_agent_defined,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _fixture(name: str) -> str:
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


# The two launch screens the service-level launch tests use, mirrored here so the
# provider-level preservation tests anchor to the same real layouts (a fallback
# that must stay identity_mismatch, and a healthy single-agent header that must
# confirm immediately). Kept byte-for-byte in sync with
# test/services/test_kiro_agent_identity_launch.py.
_FALLBACK_SCREEN = (
    "% kiro-cli chat --agent-engine v2 --trust-all-tools --agent developer\n"
    '● agent "developer" not found, using "kiro_default"\n'
    "kiro_default · auto · ◔ 4% · Midway: 10h 20m\n"
    "›  ask a question or describe a task ↵\n"
)
_HEALTHY_SCREEN = (
    "% kiro-cli chat --agent-engine v2 --trust-all-tools --agent developer\n"
    "developer · auto · ◔ 4% · Midway: 10h 20m\n"
    "›  ask a question or describe a task ↵\n"
)


def _kiro_captures() -> List[Path]:
    """Real Kiro captures only; a reconstructed screen says so in its name."""
    return sorted(p for p in FIXTURES_DIR.glob("kiro_cli_*.txt") if "reconstructed" not in p.name)


# Header lines drawn from the real fixtures / measured layouts, used as building
# blocks for randomly assembled panes in the parser-stability property. Each line
# is a single column-0 header row that header_agents() resolves to one agent.
_FIXTURE_HEADER_LINES = [
    "developer · auto · ◔ 4% · Midway: 10h 20m",
    "developer · claude-opus-4.6-1m · ◔ 3%  ~/project · (main)",
    "kiro_default · auto · ◔ 4% · Midway: 10h 20m",
    "reviewer · auto · ◑ 52% · auth ok",
    "code_supervisor · auto · ◔ 2% · Midway: 9h 59m",
    "sk0-readonly · claude-opus-5.5 · ◔ 1% · Midway: 10h 23m",
]

# Non-header noise rows that must never resolve to an agent (chrome, prompts,
# the echoed launch command, the fallback warning, an indented header-shape row).
_FIXTURE_NOISE_LINES = [
    "",
    "─" * 60,
    " Trust All Tools active, confirmations are off · /quit to exit",
    "›  ask a question or describe a task ↵",
    "% kiro-cli chat --agent-engine v2 --trust-all-tools --agent developer",
    '● agent "developer" not found, using "kiro_default"',
    "   kiro_default · auto · ◔ 4%",  # indented -> not a header row
    "~/project · (main)",
]


# =============================================================================
# Property group 1: require_header_identity is a stable verdict of its inputs.
#
# The fix never changes how this decision function is called; it only changes
# how long the poll runs before calling it with the last read's observation.
# Pinning the verdict for random observations pins that the fix cannot alter the
# meaning of the inputs it eventually passes in.
# =============================================================================

# Names a generated observation can contain: the requested agent, other full
# agents, the fallbacks, plan mode's Plan header, and partial (elided) names.
_REQUESTED = "developer"
_FULL_NAMES = ["developer", "reviewer", "code_supervisor", "sk0-readonly"]
_FALLBACK_NAMES = sorted(KIRO_FALLBACK_AGENTS)
_PARTIAL_NAMES = ["develo…", "code_supervi...", "…ndling-review"]
_OBSERVED_TOKENS = _FULL_NAMES + _FALLBACK_NAMES + _PARTIAL_NAMES + [PLAN_MODE_AGENT]


def _verdict(expected: str, observed: List[str], *, plan_mode: bool):
    """The decision function's outcome as a comparable value: ('ok',) on pass,
    or ('error', kind) on refusal. Used to assert stability across calls."""
    try:
        require_header_identity(expected, observed, plan_mode=plan_mode)
    except KiroAgentIdentityError as exc:
        return ("error", exc.kind)
    return ("ok",)


class TestRequireHeaderIdentityVerdictIsStable:
    """The decision function (unchanged by the fix) is a pure function of inputs."""

    @settings(max_examples=300, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(
        observed=st.lists(st.sampled_from(_OBSERVED_TOKENS), min_size=0, max_size=4),
        plan_mode=st.booleans(),
    )
    def test_verdict_is_deterministic_for_random_observations(self, observed, plan_mode):
        """Validates: Requirements 3.1, 3.2, 3.3, 3.4

        Re-invoking require_header_identity with the same inputs yields the same
        verdict. Since the fix does not touch this function, recording the
        verdict now pins the baseline the fixed code must reproduce.
        """
        first = _verdict(_REQUESTED, list(observed), plan_mode=plan_mode)
        second = _verdict(_REQUESTED, list(observed), plan_mode=plan_mode)
        assert first == second

    @settings(max_examples=300)
    @given(observed=st.lists(st.sampled_from(_OBSERVED_TOKENS), min_size=0, max_size=4))
    def test_the_only_pass_is_a_unanimous_full_requested_agent(self, observed):
        """Validates: Requirements 3.1, 3.2, 3.3, 3.6

        The verdict passes exactly when every observed name is the requested
        agent in full (the fast/healthy path); every other shape fails closed.
        This is the invariant the fix must not loosen.
        """
        names = list(dict.fromkeys(observed))
        full = [n for n in names if not is_partial_agent_name(n)]
        expected_pass = bool(names) and all(n == _REQUESTED for n in names) and full == names
        verdict = _verdict(_REQUESTED, list(observed), plan_mode=False)
        assert (verdict == ("ok",)) == expected_pass

    @pytest.mark.parametrize(
        ("observed", "kind"),
        [
            (["kiro_default"], "identity_mismatch"),  # wrong (fallback) agent
            (["reviewer"], "identity_mismatch"),  # wrong agent
            (["developer", "kiro_default"], "identity_mismatch"),  # multi-agent
            (["kiro_default", "developer"], "identity_mismatch"),  # multi-agent, other order
            (["develo…"], "header_not_found"),  # partial name only
            ([], "header_not_found"),  # genuine absence
        ],
    )
    def test_canonical_fail_closed_shapes(self, observed, kind):
        """Validates: Requirements 3.1, 3.2, 3.3

        The named fail-closed shapes keep their exact error kind.
        """
        assert _verdict(_REQUESTED, observed, plan_mode=False) == ("error", kind)

    def test_plan_mode_header_for_another_agent_is_a_mismatch(self):
        """Validates: Requirements 3.4"""
        assert _verdict(_REQUESTED, [PLAN_MODE_AGENT], plan_mode=True) == (
            "error",
            "identity_mismatch",
        )


# =============================================================================
# Property group 2: _verify_agent_identity observable outcomes on unfixed code.
#
# Only the two outcomes already reachable on the UNFIXED code are asserted here:
#   - a header present on the FIRST read confirms with exactly one read (fast
#     path, no added latency);
#   - a header that NEVER renders refuses with header_not_found and sends no key.
# The within-budget-delay confirm is validated after the fix (task 3.2).
# =============================================================================


def _provider(agent: str = "developer") -> KiroCliProvider:
    return KiroCliProvider("pv000001", "pv-session", "pv-window", agent)


class TestVerifyAgentIdentityObservableOutcomes:
    @pytest.mark.asyncio
    @settings(max_examples=25, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(data=st.data())
    async def test_a_full_header_on_the_first_read_confirms_with_no_extra_reads(self, data):
        """Validates: Requirements 3.6

        Fast path: when a full single-agent header for the requested agent is on
        the very first read, identity confirms immediately after exactly one
        read, and no keys are sent to the pane. This is the "no added latency"
        guarantee the fix must preserve.
        """
        header_line = data.draw(st.sampled_from(_FIXTURE_HEADER_LINES))
        agent = header_agents(header_line)[0]
        # Skip fallback-named headers: those are a mismatch, covered elsewhere.
        if agent in KIRO_FALLBACK_AGENTS:
            return
        screen = f"% kiro-cli chat --agent {agent}\n{header_line}\n›  ask a question ↵\n"
        backend = MagicMock()
        backend.get_history.return_value = screen
        provider = _provider(agent)
        with (
            patch(
                "cli_agent_orchestrator.providers.kiro_cli.get_backend", return_value=backend
            ),
            patch(
                "cli_agent_orchestrator.providers.kiro_cli.IDENTITY_HEADER_READ_INTERVAL_S", 0
            ),
        ):
            await provider._verify_agent_identity()
        assert provider.identity_verified is True
        assert backend.get_history.call_count == 1
        backend.send_keys.assert_not_called()
        backend.send_special_key.assert_not_called()

    @pytest.mark.asyncio
    async def test_the_healthy_screen_confirms_on_the_first_read(self):
        """Validates: Requirements 3.6

        The real _HEALTHY_SCREEN layout confirms immediately with one read.
        """
        backend = MagicMock()
        backend.get_history.return_value = _HEALTHY_SCREEN
        provider = _provider("developer")
        with patch(
            "cli_agent_orchestrator.providers.kiro_cli.get_backend", return_value=backend
        ):
            await provider._verify_agent_identity()
        assert provider.identity_verified is True
        assert backend.get_history.call_count == 1
        backend.send_keys.assert_not_called()

    @pytest.mark.asyncio
    async def test_the_fallback_screen_is_an_identity_mismatch(self):
        """Validates: Requirements 3.1

        The real _FALLBACK_SCREEN (kiro_default header, developer requested)
        refuses with identity_mismatch on the first read and sends no keys.
        """
        backend = MagicMock()
        backend.get_history.return_value = _FALLBACK_SCREEN
        provider = _provider("developer")
        with (
            patch(
                "cli_agent_orchestrator.providers.kiro_cli.get_backend", return_value=backend
            ),
            pytest.raises(KiroAgentIdentityError) as excinfo,
        ):
            await provider._verify_agent_identity()
        assert excinfo.value.kind == "identity_mismatch"
        assert provider.identity_verified is False
        backend.send_keys.assert_not_called()
        backend.send_special_key.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_header_that_never_renders_refuses_with_header_not_found(self):
        """Validates: Requirements 2.4

        Genuine absence: when the screen never shows a header row, the poll
        exhausts its reads and refuses with header_not_found, sending no keys.
        On the unfixed code the read count is exactly IDENTITY_HEADER_READS; the
        fix extends how long it polls but must still fail closed here.
        """
        headerless = (
            "% kiro-cli chat --agent-engine v2 --trust-all-tools --agent developer\n"
            "●  Initializing...\n"
            "›  ask a question or describe a task ↵\n"
        )
        backend = MagicMock()
        backend.get_history.return_value = headerless
        provider = _provider("developer")
        with (
            patch(
                "cli_agent_orchestrator.providers.kiro_cli.get_backend", return_value=backend
            ),
            patch(
                "cli_agent_orchestrator.providers.kiro_cli.IDENTITY_HEADER_READ_INTERVAL_S", 0
            ),
            pytest.raises(KiroAgentIdentityError) as excinfo,
        ):
            await provider._verify_agent_identity()
        assert excinfo.value.kind == "header_not_found"
        assert backend.get_history.call_count == IDENTITY_HEADER_READS
        assert provider.identity_verified is False
        backend.send_keys.assert_not_called()
        backend.send_special_key.assert_not_called()

    @pytest.mark.asyncio
    async def test_pre_launch_fallback_and_undefined_agents_are_refused_before_any_window(
        self, tmp_path
    ):
        """Validates: Requirements 3.5

        The pre-launch agent-store check refuses a fallback name
        (fallback_agent) and an undefined name (agent_not_found) before any poll
        window exists. This path is untouched by the fix.
        """
        for fallback in sorted(KIRO_FALLBACK_AGENTS):
            with pytest.raises(KiroAgentIdentityError) as excinfo:
                require_kiro_agent_defined(fallback, [tmp_path])
            assert excinfo.value.kind == "fallback_agent"

        with pytest.raises(KiroAgentIdentityError) as excinfo:
            require_kiro_agent_defined("not-installed-agent", [tmp_path])
        assert excinfo.value.kind == "agent_not_found"


# =============================================================================
# Property group 3: header_agents() is a pure function of the pane text.
#
# The parser is untouched by the fix. Random panes assembled from the fixture
# header lines and noise rows must parse to the same, order-preserving, deduped
# agent list — the contract the fix relies on staying constant.
# =============================================================================


class TestHeaderAgentsParserIsStable:
    @settings(max_examples=300)
    @given(
        rows=st.lists(
            st.sampled_from(_FIXTURE_HEADER_LINES + _FIXTURE_NOISE_LINES),
            min_size=0,
            max_size=8,
        )
    )
    def test_parse_is_deterministic_and_matches_expected_agents(self, rows):
        """Validates: Requirements 3.6

        header_agents() is deterministic, and its output is exactly the
        column-0 header rows' agents in first-seen order, deduped — independent
        of the noise rows interleaved around them.
        """
        pane = "\n".join(rows)
        first = header_agents(pane)
        second = header_agents(pane)
        assert first == second

        expected: List[str] = []
        for row in rows:
            if row in _FIXTURE_HEADER_LINES:
                agent = header_agents(row)[0]
                if agent not in expected:
                    expected.append(agent)
        assert first == expected

    @settings(max_examples=200)
    @given(
        rows=st.lists(
            st.sampled_from(_FIXTURE_HEADER_LINES + _FIXTURE_NOISE_LINES),
            min_size=0,
            max_size=8,
        )
    )
    def test_identity_readable_agrees_with_a_full_name_being_present(self, rows):
        """Validates: Requirements 3.3

        identity_readable() is true exactly when some parsed name is full. The
        fixture header lines are all full names, so any header row makes the
        pane readable.
        """
        pane = "\n".join(rows)
        observed = header_agents(pane)
        assert identity_readable(observed) == any(
            not is_partial_agent_name(n) for n in observed
        )

    @pytest.mark.parametrize("fixture", [p.name for p in _kiro_captures()])
    def test_real_captures_parse_to_only_full_names(self, fixture):
        """Validates: Requirements 3.6

        Every real capture parses with no partial names (the fix does not change
        the parser, so these stay stable).
        """
        text = _fixture(fixture)
        assert all(not is_partial_agent_name(n) for n in header_agents(text))
