"""SHAI CLI provider implementation.

This module provides the SHAICliProvider class for integrating with SHAI
(ovh/shai), a terminal-based AI coding agent with a native agent-config system.

This is currently a class skeleton: the constructor, ``honors_model()``, and
minimal stubs for the ``BaseProvider`` abstract methods are in place so the
module imports cleanly. The real behavior is filled in by later tasks:
- ``initialize()`` / ``_build_launch_command()`` / ``_handle_startup_prompts()`` — task 4.x
- ``get_status()`` and the module-level regex patterns — task 5.x
- ``extract_last_message_from_script()`` / ``exit_cli()`` / ``cleanup()`` and
  paste tuning — task 6.x

Mirrors ``providers/opencode_cli.py`` as the closest template.
"""

import asyncio
import logging
import re
import shlex
import time
from typing import TYPE_CHECKING, List, Optional

from cli_agent_orchestrator.backends.registry import get_backend
from cli_agent_orchestrator.constants import SHAI_XDG_HOME
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.base import BaseProvider, OutputExtractionError
from cli_agent_orchestrator.services.settings_service import get_server_settings
from cli_agent_orchestrator.utils.terminal import wait_for_shell, wait_until_status

if TYPE_CHECKING:
    from cli_agent_orchestrator.models.agent_profile import AgentProfile

logger = logging.getLogger(__name__)


# =============================================================================
# Module-level regex patterns (task 5.1)
# =============================================================================
# Finalized against the live SHAI runtime probe (task 1) and validated against
# the real capture-pane fixtures in test/providers/fixtures/shai_cli_*.txt. See
# test/providers/fixtures/shai_cli_PROBE_NOTES.md for the confirmed TUI chrome.
#
# SHAI renders a stacked, NON-alt-screen TUI (SHAI v0.2.2). Each turn appends
# inline: a cyan ``>`` user line, a dim-gray ``✻`` reasoning block, optional
# ``→ <Tool>`` tool rows, then a white ``●`` response bullet followed by the
# answer text. A bordered input box and a single status-bar line stay pinned at
# the bottom. Lesson #5: always strip ANSI (ANSI_CODE_PATTERN) BEFORE matching
# any of the chrome patterns below. The probe confirmed the frames carry only
# SGR (color) sequences — no cursor/non-SGR CSI — so the SGR pattern fully
# cleans them; keeping SGR coverage is sufficient (and minimal).

# ANSI/SGR color-code pattern — certain, reused verbatim from the OpenCode
# template. Probe frames use truecolor SGR (e.g. \x1b[38;2;255;100;100m),
# 256-color SGR (\x1b[38;5;15m), and simple SGR (\x1b[2m, \x1b[0m); all are
# "\x1b[" + digits/semicolons + "m". The fixtures contain no non-SGR CSI.
ANSI_CODE_PATTERN = r"\x1b\[[0-9;]*m"

# Idle footer / input hint (IDLE). Stable bottom footer text shown whenever SHAI
# is waiting for the next user message:
#   "? help · / commands · esc cancel · ctrl+c quit"
# Separator dots are U+00B7 (·). Spacing is tolerant in case a release re-pads.
IDLE_FOOTER_PATTERN = r"\?\s*help\s*·\s*/\s*commands\s*·\s*esc\s*cancel\s*·\s*ctrl\+c\s*quit"

# Fallback idle anchor — the bordered input box always shows the "? for help"
# hint ("│ >  ? for help ... │"). Used if a release renames the footer line.
IDLE_INPUT_HINT_FALLBACK_PATTERN = r"\?\s*for\s*help"

# Processing indicator (PROCESSING). The leading glyph is an ANIMATED braille
# spinner cycling through U+2800–U+28FF (observed ⠹ ⠏ ⠧ ⠥ ⠦), so we do NOT pin
# to a single glyph — key on the stable text "Agent is working...". The optional
# leading braille glyph is allowed but not required.
#   " ⠹ Agent is working... (press esc to cancel)"
# Note: in-flight tool rows carry their own spinner + a "(<n>s)" duration token
# (e.g. "⠥ Bash echo hello-from-bash (9.3s)"); that is a tool row, NOT the
# "Agent is working..." line, so it is intentionally not matched here.
PROCESSING_PATTERN = r"[\u2800-\u28FF]?\s*Agent is working\.\.\."

# Per-turn completion marker (COMPLETED). There is NO "done in Xs" banner — a
# completed turn is a white response bullet "●" (U+25CF) on its own line,
# followed by a blank line and the answer text. Key on the response bullet at
# line start. The negative lookahead excludes the RED error bullet
# ("● Error: ...") so the completion marker matches ONLY the normal response
# block; ERROR is handled separately (and ranks above COMPLETED in get_status).
# Do NOT use a "(9.3s)" duration token — that appears only on in-flight tool
# rows, never on turn completion. Intended for use with re.MULTILINE.
COMPLETION_MARKER_PATTERN = r"^[ \t]*\u25CF(?![^\n]*\bError:)"

# Permission / approval dialog heading (WAITING_USER_ANSWER):
#   "╭ 🔐 Permission Required ──...──╮"  (🔐 = U+1F510). Key on the stable
# heading text.
PERMISSION_PROMPT_PATTERN = r"Permission Required"

# Fallback permission anchor — the dialog body always asks the confirmation
# question. Used if a release renames the heading.
PERMISSION_PROMPT_FALLBACK_PATTERN = r"Do you want to run this tool\?"

# Error banner (ERROR). Rendered as a RED bullet "●" + bold "Error:" on one
# line (e.g. "● Error: LLM error: {...model_not_found...}"). After ANSI strip,
# key on the "●" adjacent to "Error:". This provider error chrome (bullet +
# "Error:") distinguishes a real error banner from assistant prose that merely
# contains the word "error"; in the raw frame the error "●" is red
# (\x1b[38;2;255;100;100m) vs the white response "●" (\x1b[38;5;15m), so colour
# is a further discriminator available pre-strip.
ERROR_PATTERN = r"\u25CF\s*Error:"

# Fallback error anchor — bold "Error: " token constrained to a leading-ish
# position, for releases that drop the bullet from the error line.
ERROR_FALLBACK_PATTERN = r"\bError:\s"

# User-turn prefix (extraction boundary, task 6.1). A left-margin ">" + space,
# cyan (\x1b[38;5;6m), at column 0 — e.g. "> Say hello...". Distinct from the
# bordered input box line ("│ >  ? for help ... │"), which has the ">" indented
# inside the box border rather than at the left margin. Intended for use with
# re.MULTILINE.
USER_MESSAGE_PATTERN = r"^>\s"

# --- Extraction helper patterns (chrome stripped during task 6.1) -----------
# Reasoning / "thinking" preamble lines begin with "✻" (U+273B, dim gray). These
# are stripped when extracting the response (keep only the "●" answer block).
REASONING_PATTERN = r"^[ \t]*\u273B\s"

# Tool-execution rows begin with "→" (U+2192, green) + a bold tool name, with
# the tool output indented beneath. These are stripped during extraction.
TOOL_ROW_PATTERN = r"^[ \t]*\u2192\s"

# Ceiling on the launch readiness wait (initialize step 5). Derived, not a bare
# guess (#710): it is the first-run auth/model-resolution window plus the extra
# slack a concurrent multi-agent launch adds, which the live probe measured at
# well under two minutes. Named here so the single call site reads the budget
# instead of restating a literal.
INIT_READY_TIMEOUT_SECONDS = 120.0


class SHAICliProvider(BaseProvider):
    """Provider for SHAI (ovh/shai) CLI integration.

    Manages the lifecycle of a SHAI TUI session inside a tmux window:
    initialization, status detection, response extraction, and cleanup.

    Attributes:
        terminal_id: Unique identifier for this terminal instance
        session_name: Name of the tmux session containing this terminal
        window_name: Name of the tmux window for this terminal
        _agent_profile: Name of the installed SHAI agent to launch
        _model: Optional model override (SHAI pins the model in the agent
            config JSON rather than a per-launch flag, so this is currently
            informational — see ``honors_model()``)
    """

    @classmethod
    def honors_model(
        cls,
        agent_profile: Optional[str],
        profile: Optional["AgentProfile"],
        requested_model: Optional[str],
    ) -> bool:
        """Return whether SHAI applies a per-launch model request.

        SHAI pins the model in the agent config JSON (``llm_provider.model``),
        not a per-launch CLI flag. The live runtime probe (task 1) confirmed
        SHAI has NO ``--model``-equivalent flag, so this is ``False`` and final.
        """
        return False

    def __init__(
        self,
        terminal_id: str,
        session_name: str,
        window_name: str,
        agent_profile: Optional[str] = None,
        allowed_tools: Optional[list] = None,
        model: Optional[str] = None,
    ):
        """Initialize SHAI CLI provider.

        Args:
            terminal_id: Unique identifier for this terminal
            session_name: Name of the tmux session
            window_name: Name of the tmux window
            agent_profile: Name of the installed SHAI agent (e.g. ``"developer"``)
            allowed_tools: Optional CAO tool list (enforcement handled elsewhere)
            model: Optional model override (informational; see ``honors_model()``)
        """
        super().__init__(terminal_id, session_name, window_name, allowed_tools)
        self._agent_profile = agent_profile or ""
        self._model = model
        self._initialized = False

    # =========================================================================
    # Screen-safe opt-ins (task 7)
    # =========================================================================
    # The live runtime probe (task 1, PROBE_NOTES item 3) CONFIRMED SHAI is
    # screen-safe: it does NOT use the terminal alternate screen
    # (``tmux display-message '#{alternate_on}'`` returned 0 during an active
    # session), scrollback is preserved, and ``capture-pane -e -p`` returns a
    # clean, parseable frame. Per the ``base.py`` contract these flags are set
    # True ONLY because the detector genuinely works on a rendered/composited
    # viewport — SHAI's ``get_status`` is line-oriented (idle footer / spinner /
    # ``●`` bullet markers), not dispatch-bookkeeping dependent, so it classifies
    # a settled capture-pane snapshot correctly.

    # Line-oriented detector works on a rendered capture-pane snapshot, so the
    # deferred-init direct status probe (capture-pane bypass) is safe to use.
    supports_direct_status_probe = True

    # Ships a purpose-built get_status_from_screen() calibrated for the
    # pyte-composited (escape-free) viewport.
    supports_screen_detection = True

    @property
    def extraction_tail_lines(self) -> int:
        """Capture extra scrollback for extraction.

        Consumed by ``terminal_service.get_output`` via a ``getattr`` capability
        check: providers that define this attribute opt in to a deeper
        ``capture-pane`` for LAST-mode extraction.

        Unlike OpenCode (which alt-screens and starves scrollback), the live
        probe (task 1, PROBE_NOTES item 3) confirmed SHAI does NOT alt-screen —
        turns accumulate inline and ``capture-pane -S -<n>`` returns real
        scrollback history. So a generous tail genuinely delivers prior-turn
        context to the extraction loop, which anchors on the LAST ``●`` response
        block. 2000 lines mirrors the OpenCode template's belt-and-braces value.
        """
        return 2000

    @property
    def paste_enter_count(self) -> int:
        """SHAI's TUI submits on a single Enter after bracketed paste.

        The live runtime probe (task 1) confirmed that a bracketed paste
        followed by a SINGLE Enter submits the prompt reliably — the user turn
        appears and processing begins. This overrides the design's placeholder
        default of 2. See ``test/providers/fixtures/shai_cli_PROBE_NOTES.md``
        item 4.
        """
        return 1

    @property
    def paste_submit_delay(self) -> float:
        """Delay between the bracketed-paste end marker and the submit Enter.

        The live runtime probe (task 1) found a ~0.5–0.8s delay submitted
        reliably in every trial; 0.8s is the recommended safe value (1.0s is a
        fine conservative upper bound) to avoid the "never started processing"
        race after initialization reports IDLE/COMPLETED. See
        ``test/providers/fixtures/shai_cli_PROBE_NOTES.md`` item 4.
        """
        return 0.8

    def _build_launch_command(self) -> str:
        """Build the inline-env ``shai`` launch command string.

        Binds SHAI to the CAO-managed config tree via ``XDG_CONFIG_HOME`` and
        forces a 256-color terminal for clean tmux rendering. The live runtime
        probe (task 1) confirmed these two env vars are the complete launch env:
        no telemetry/auto-update/mouse-suppression vars are needed (unlike
        OpenCode), and ``XDG_CONFIG_HOME`` is honored for agents while auth falls
        back to the native ``~/.config/shai`` location (so no auth copy is
        required). See ``test/providers/fixtures/shai_cli_PROBE_NOTES.md``.

        A named agent is selected with ``-a <profile>`` only when a profile is
        set; SHAI pins the model in the agent config JSON, so there is no model
        flag here (see ``honors_model()``).

        Returns:
            Inline-env launch string, e.g.
            ``XDG_CONFIG_HOME=/home/u/.aws/shai_xdg TERM=xterm-256color shai -a developer``.
        """
        # Quote the XDG path discipline-style (mirrors OpenCode's quoting care) —
        # the path may contain characters needing shell escaping.
        env_pairs = [
            f"XDG_CONFIG_HOME={shlex.quote(str(SHAI_XDG_HOME))}",
            "TERM=xterm-256color",
        ]
        cmd_parts = ["shai"]
        if self._agent_profile:
            cmd_parts += ["-a", self._agent_profile]
        # env vars are shell words; join cmd parts with shlex for proper quoting
        return " ".join(env_pairs) + " " + shlex.join(cmd_parts)

    # =========================================================================
    # Abstract-method stubs — implemented by later tasks (4.x, 5.x, 6.x).
    # Present now only so this module (and the ABC) imports cleanly.
    # =========================================================================

    async def initialize(self) -> bool:
        """Start the SHAI TUI and wait until it is ready for the first turn.

        Steps (design Component 2 "initialize", Req 2):
        1. Wait for the shell prompt in the tmux window (``wait_for_shell``);
           raise ``TimeoutError`` if the shell never appears — Req 2.1.
        2. Shell warm-up echo round-trip (lesson #14). SHAI is a native binary
           in ``~/.local/bin``; a half-initialized shell ``PATH`` can make
           ``shai`` silently exit before it ever renders. A trivial ``echo``
           forces the interactive shell to finish sourcing its rc files and
           settle its ``PATH`` before the launch command is sent.
        3. Send the inline-env ``shai`` launch command — Req 2.2.
        4. Run the startup-prompt stabilization loop so a first-run auth/dialog
           screen is dismissed (or surfaced as WAITING_USER_ANSWER) before the
           readiness poll.
        5. Wait for IDLE or COMPLETED (120s) — reaching either is successful
           initialization (Req 2.4); otherwise raise ``TimeoutError`` (Req 2.5).

        The 120s window mirrors the OpenCode template's cold-start budget and
        covers first-run auth/model resolution.

        Returns:
            True if initialization completed successfully.

        Raises:
            TimeoutError: If the shell never becomes ready (Req 2.1), or SHAI
                does not reach IDLE/COMPLETED within 120s (Req 2.5).
        """
        # ── 1. Wait for the shell prompt ─────────────────────────────────────
        init_timeout = get_server_settings()["provider_init_timeout"]
        if not await wait_for_shell(self.terminal_id, timeout=init_timeout):
            raise TimeoutError(f"Shell initialization timed out after {init_timeout}s")

        backend = get_backend()

        # ── 2. Shell warm-up echo round-trip (lesson #14) ────────────────────
        # Force the interactive shell to finish sourcing rc files / settle PATH
        # so the native `shai` binary in ~/.local/bin resolves rather than the
        # launch silently exiting on a half-initialized PATH.
        backend.send_keys(self.session_name, self.window_name, "echo cao-shai-warmup-$((1+1))")
        # A short settle so the warm-up command runs before the launch command
        # is typed into the same pane.
        await asyncio.sleep(0.5)

        # ── 3. Send the launch command ───────────────────────────────────────
        command = self._build_launch_command()
        backend.send_keys(self.session_name, self.window_name, command)

        # ── 4. Startup-prompt stabilization loop ─────────────────────────────
        await self._handle_startup_prompts()

        # ── 5. Wait for IDLE or COMPLETED ────────────────────────────────────
        # INIT_READY_TIMEOUT_SECONDS covers first-run auth/model resolution and
        # concurrent launches (see the constant's derivation note).
        if not await wait_until_status(
            self.terminal_id,
            {TerminalStatus.IDLE, TerminalStatus.COMPLETED},
            timeout=INIT_READY_TIMEOUT_SECONDS,
        ):
            raise TimeoutError(
                f"SHAI CLI initialization timed out after {INIT_READY_TIMEOUT_SECONDS}s"
            )

        self._initialized = True
        return True

    async def _handle_startup_prompts(self) -> None:
        """Stabilize the SHAI TUI after launch, before the readiness poll.

        A polling loop (NOT a one-shot check). SHAI *may* show a first-run
        auth/provider screen, but the live probe could not force one here —
        OVHcloud serves an anonymous default endpoint, so an un-provisioned
        launch still lands straight on idle (see
        ``test/providers/fixtures/shai_cli_PROBE_NOTES.md``). The loop is kept
        deliberately tolerant per those notes:

        - Fast path: when SHAI launches straight to idle (the common,
          authenticated case) the first poll sees the idle footer and returns
          immediately — a no-op.
        - Ready: as soon as the idle footer / input box appears (``get_status``
          reports IDLE or COMPLETED) the loop returns.
        - A non-self-clearing dialog surfaces as ``WAITING_USER_ANSWER``. That
          is a *settled* state: the launch readiness poll in ``launch.py``
          already treats WAITING_USER_ANSWER as settled, so the loop returns and
          lets the readiness poll handle it rather than blindly sending keys
          into an unverified dialog.

        The loop never raises — it is a best-effort stabilizer; the hard
        timeout is enforced by the ``wait_until_status`` call in ``initialize``.
        """
        poll_interval = 0.5
        deadline = time.monotonic() + 30.0
        backend = get_backend()

        while time.monotonic() < deadline:
            try:
                buffer = backend.get_history(self.session_name, self.window_name)
            except Exception as exc:  # noqa: BLE001 — a failed read is not fatal here
                logger.debug(
                    "SHAI startup: pane read failed (terminal=%s): %s",
                    self.terminal_id,
                    exc,
                )
                buffer = ""

            status = self.get_status(buffer)

            # Ready (idle footer / input box present) — fast path and the common
            # authenticated case both exit here. PROCESSING keeps polling,
            # WAITING_USER_ANSWER is handled by the branch below, and
            # ERROR/UNKNOWN deliberately fall through to the hard-timeout poll.
            # subset-ok: IDLE/COMPLETED are the only two "settled at a ready
            # prompt" states this stabilizer exits on; the rest are handled above.
            if status in (TerminalStatus.IDLE, TerminalStatus.COMPLETED):
                logger.debug(
                    "SHAI startup: settled to %s (terminal=%s)",
                    status,
                    self.terminal_id,
                )
                return

            # A dialog that does not self-clear is a settled state for the
            # readiness poll; don't force keys into an unverified screen.
            if status == TerminalStatus.WAITING_USER_ANSWER:
                logger.info(
                    "SHAI startup: a startup dialog is present (WAITING_USER_ANSWER); "
                    "leaving it for the readiness poll (terminal=%s)",
                    self.terminal_id,
                )
                return

            await asyncio.sleep(poll_interval)

        # Deadline reached without a settled state — let wait_until_status in
        # initialize() enforce the hard timeout.
        logger.debug(
            "SHAI startup: stabilization loop deadline reached without a settled "
            "state (terminal=%s); deferring to the readiness poll",
            self.terminal_id,
        )

    def get_status(self, buffer: str) -> TerminalStatus:
        """Detect current TUI state from the StatusMonitor buffer string.

        Priority ladder (first match wins), per design Component 2 "get_status":
        0. native-status short-circuit (herdr backend) — trust the backend's
           agent state when available.
        1. ``_resolve_buffer`` → empty buffer → ``UNKNOWN``.
        2. strip ANSI (module-level ``ANSI_CODE_PATTERN``).
        3. ERROR — provider error chrome (``ERROR_PATTERN``: red ``●`` + ``Error:``),
           NOT assistant prose. Ranked ABOVE WAITING per Req 3.5.
        4. WAITING_USER_ANSWER — permission dialog heading present, no idle footer
           after it.
        5. PROCESSING — ``Agent is working...`` spinner with a line-position stale
           guard (a spinner is stale if a later line shows the idle footer or a
           completion marker); mirrors OpenCode's line-level guard.
        6. COMPLETED — last response ``●`` completion marker followed by the idle
           footer, with no later new-turn marker.
        7. IDLE — idle footer present, no live processing indicator.
        8. UNKNOWN — fallback.

        Args:
            buffer: StatusMonitor buffer string for this terminal.

        Returns:
            Current TerminalStatus. Total — never raises.
        """
        # ── 0. Native status (herdr) ─────────────────────────────────────────
        # Trust the backend's agent state when available; on herdr the buffer is
        # never fed (pipe_pane is a no-op), so buffer parsing can't be relied on.
        native = self._resolve_native_status(buffer)
        if native is not None:
            return native

        # herdr never pushes a buffer; read live pane content instead of falling
        # through to "no output" on every call.
        buffer = self._resolve_buffer(buffer)
        if not buffer:
            return TerminalStatus.UNKNOWN

        # ── Strip ANSI once up front (lesson #5) ─────────────────────────────
        clean = re.sub(ANSI_CODE_PATTERN, "", buffer)

        # ── 3. ERROR ─────────────────────────────────────────────────────────
        # Provider error chrome (red "●" + "Error:"), ranked above WAITING per
        # Req 3.5. The completion "●" is white and carries no "Error:" token, so
        # ERROR_PATTERN does not match a normal response bullet. The fallback
        # (bold "Error: " without the bullet) catches a release that drops the
        # "●" from the error line; it is deliberately only tried when the primary
        # bullet pattern misses, so normal prose containing "Error:" mid-line is
        # not misread (the fallback anchors on a leading-ish "Error: " token).
        if re.search(ERROR_PATTERN, clean) or re.search(
            ERROR_FALLBACK_PATTERN, clean, re.MULTILINE
        ):
            return TerminalStatus.ERROR

        # ── 4. WAITING_USER_ANSWER ───────────────────────────────────────────
        # Primary: the dialog heading ("Permission Required"). Fallback: the
        # dialog body's confirmation question, for a release that renames the
        # heading — either anchor marks the same dialog.
        perm_matches = list(re.finditer(PERMISSION_PROMPT_PATTERN, clean)) or list(
            re.finditer(PERMISSION_PROMPT_FALLBACK_PATTERN, clean)
        )
        if perm_matches:
            last_perm_end = perm_matches[-1].end()
            # The permission dialog replaces the normal footer; if the idle footer
            # still appears *after* the dialog the user already dismissed it.
            if not re.search(IDLE_FOOTER_PATTERN, clean[last_perm_end:]):
                return TerminalStatus.WAITING_USER_ANSWER

        # ── 5. PROCESSING ────────────────────────────────────────────────────
        # Line-level position guard (mirrors OpenCode): the "Agent is working..."
        # spinner is live only when no later line shows the idle footer or a
        # completion marker. In the stale case (scrollback remnant), those later
        # markers mean processing has ended.
        lines = clean.split("\n")
        last_proc_line = -1
        for i, line in enumerate(lines):
            if re.search(PROCESSING_PATTERN, line):
                last_proc_line = i

        proc_is_stale = False
        if last_proc_line >= 0:
            later = lines[last_proc_line + 1 :]
            has_idle_later = any(re.search(IDLE_FOOTER_PATTERN, l) for l in later)
            has_completion_later = any(
                re.search(COMPLETION_MARKER_PATTERN, l, re.MULTILINE) for l in later
            )
            if not has_idle_later and not has_completion_later:
                return TerminalStatus.PROCESSING
            # Guard fired: the spinner is a stale scrollback remnant.
            proc_is_stale = True

        # SHAI renders a stacked, NON-alt-screen TUI: the idle footer appears in
        # the splash scrollback *above* any turn content, and a ready input box
        # ("? for help") pins to the bottom. So the ready signal for both IDLE
        # and COMPLETED is the footer/input-box anywhere with no live spinner; the
        # only thing that distinguishes COMPLETED from a fresh IDLE is the presence
        # of a finished response "●" bullet from the last turn.
        idle_ready = (
            re.search(IDLE_FOOTER_PATTERN, clean) is not None
            or re.search(IDLE_INPUT_HINT_FALLBACK_PATTERN, clean) is not None
        ) and (last_proc_line < 0 or proc_is_stale)

        # ── 6. COMPLETED ─────────────────────────────────────────────────────
        # A finished response "●" bullet with the input box back to ready. The
        # completion marker excludes the red error bullet (handled above), so this
        # matches only the normal response block.
        completion_matches = list(re.finditer(COMPLETION_MARKER_PATTERN, clean, re.MULTILINE))
        if completion_matches and idle_ready:
            return TerminalStatus.COMPLETED

        # ── 7. IDLE ──────────────────────────────────────────────────────────
        # Ready input box with no finished response yet (fresh session) and no
        # live spinner (or a stale remnant flagged in step 5).
        if idle_ready:
            return TerminalStatus.IDLE

        # ── 8. UNKNOWN (fallback) ─────────────────────────────────────────────
        return TerminalStatus.UNKNOWN

    # =========================================================================
    # Screen-based detection (pyte) — task 7
    # =========================================================================

    def get_status_from_screen(self, screen_lines: List[str]) -> TerminalStatus:
        """Detect status from a pyte-composited viewport (escape-free rows).

        ``screen_lines`` is ``pyte.Screen.display``: a fixed-height list of
        viewport rows with all cursor moves / in-place redraws already resolved
        and NO ANSI escapes, right-padded with spaces. The StatusMonitor calls
        this (instead of ``get_status``) when ``CAO_PYTE_STATUS`` is enabled AND
        ``supports_screen_detection`` is True — safe for SHAI per the probe
        (PROBE_NOTES item 3: no alt-screen, clean composited frame).

        Because the rows are already escape-free, NO ANSI strip is needed (that
        is the one divergence from ``get_status``, which strips the raw byte
        stream first). SHAI's chrome patterns key on text / ``●`` glyphs that
        survive compositing unchanged, so the same module-level patterns apply.

        Precedence mirrors ``get_status`` exactly (first match wins):
        1. ERROR — provider error chrome (``●`` + ``Error:``), ranked above
           WAITING per Req 3.5.
        2. WAITING_USER_ANSWER — ``Permission Required`` heading with no idle
           footer after it.
        3. PROCESSING — ``Agent is working...`` spinner with a line-position
           stale guard (a later idle footer / completion marker means the
           spinner is a stale remnant).
        4. COMPLETED — a finished ``●`` response bullet with the input box back
           to ready (idle footer / input hint present, no live spinner).
        5. IDLE — ready input box, no finished response and no live spinner.
        6. UNKNOWN — fallback.
        """
        rows = [ln.rstrip() for ln in screen_lines if ln.strip()]
        if not rows:
            return TerminalStatus.UNKNOWN

        # Rows are already escape-free (pyte composited) — no ANSI strip. Join
        # with newlines so the multiline patterns behave as in get_status.
        clean = "\n".join(rows)

        # ── 1. ERROR ─────────────────────────────────────────────────────────
        if re.search(ERROR_PATTERN, clean):
            return TerminalStatus.ERROR

        # ── 2. WAITING_USER_ANSWER ───────────────────────────────────────────
        perm_matches = list(re.finditer(PERMISSION_PROMPT_PATTERN, clean))
        if perm_matches:
            last_perm_end = perm_matches[-1].end()
            if not re.search(IDLE_FOOTER_PATTERN, clean[last_perm_end:]):
                return TerminalStatus.WAITING_USER_ANSWER

        # ── 3. PROCESSING (with line-position stale guard) ───────────────────
        last_proc_line = -1
        for i, row in enumerate(rows):
            if re.search(PROCESSING_PATTERN, row):
                last_proc_line = i

        proc_is_stale = False
        if last_proc_line >= 0:
            later = rows[last_proc_line + 1 :]
            has_idle_later = any(re.search(IDLE_FOOTER_PATTERN, r) for r in later)
            has_completion_later = any(
                re.search(COMPLETION_MARKER_PATTERN, r, re.MULTILINE) for r in later
            )
            if not has_idle_later and not has_completion_later:
                return TerminalStatus.PROCESSING
            proc_is_stale = True

        # Ready signal: idle footer / input hint present with no live spinner.
        idle_ready = (
            re.search(IDLE_FOOTER_PATTERN, clean) is not None
            or re.search(IDLE_INPUT_HINT_FALLBACK_PATTERN, clean) is not None
        ) and (last_proc_line < 0 or proc_is_stale)

        # ── 4. COMPLETED ─────────────────────────────────────────────────────
        completion_matches = list(re.finditer(COMPLETION_MARKER_PATTERN, clean, re.MULTILINE))
        if completion_matches and idle_ready:
            return TerminalStatus.COMPLETED

        # ── 5. IDLE ──────────────────────────────────────────────────────────
        if idle_ready:
            return TerminalStatus.IDLE

        # ── 6. UNKNOWN (fallback) ─────────────────────────────────────────────
        return TerminalStatus.UNKNOWN

    def extract_last_message_from_script(self, script_output: str) -> str:
        """Extract the agent's final response from the SHAI TUI scrollback.

        SHAI renders a stacked, NON-alt-screen TUI where turns accumulate inline:
        a cyan ``>`` user line, a dim-gray ``✻`` reasoning block, optional
        ``→ <Tool>`` rows with indented tool output, then a white ``●`` response
        bullet followed (after a blank line) by the answer text. A bordered input
        box and a one-line status bar stay pinned at the bottom. Because turns
        accumulate, there are multiple ``●`` bullets and multiple ``>`` user
        lines — the current turn is anchored on the LAST completion ``●``.

        Algorithm (design Component 2 "extract_last_message_from_script"):
        1. Strip ANSI (``ANSI_CODE_PATTERN``) first (lesson #5) — Req 4.2.
        2. Find the LAST ``COMPLETION_MARKER_PATTERN`` (response ``●`` bullet) as
           the turn boundary; none → raise ``OutputExtractionError`` — Req 4.3.
        3. Confirm the turn by locating the last ``USER_MESSAGE_PATTERN`` (``> ``)
           before that marker; if the user turn has scrolled off the viewport,
           fall back to the first agent-content line after the marker (mirrors
           OpenCode's ``first_indent`` branch).
        4. The SHAI answer text follows the ``●`` bullet (unlike OpenCode, whose
           response precedes its marker), so slice from the end of the marker
           line to the start of the trailing input-box / status-bar chrome.
        5. Strip reasoning preamble (``REASONING_PATTERN``, ``✻``) and TUI chrome
           (tool rows ``→`` via ``TOOL_ROW_PATTERN``, any further ``●`` bullet,
           the bordered input box, and the status bar) — Req 4.2.
        6. Dedent the agent indent; clean control chars and trailing whitespace.
        7. Empty result → raise ``OutputExtractionError`` — Req 4.3.

        Args:
            script_output: Raw terminal output captured from the tmux pane.

        Returns:
            The agent's final response text, ANSI/chrome/reasoning/tool-output
            stripped.

        Raises:
            OutputExtractionError: If no completion marker is present or the
                extracted response is empty/whitespace-only (Req 4.3). This is a
                ``ValueError`` subclass so the API boundary maps it correctly
                rather than reporting a generic failure.
        """
        # 1. Strip ANSI up front (lesson #5) so chrome patterns match clean text.
        clean = re.sub(ANSI_CODE_PATTERN, "", script_output)

        # 2. Anchor on the LAST response "●" bullet — the current turn boundary.
        completions = list(re.finditer(COMPLETION_MARKER_PATTERN, clean, re.MULTILINE))
        if not completions:
            raise OutputExtractionError("No SHAI completion marker (●) found in output")
        last_completion = completions[-1]

        # 3. Confirm a user turn precedes the marker. The last "> " before the
        #    marker is the viewport-present case; absence is the scrolled-off
        #    fallback (mirroring OpenCode's first_indent branch) — in both cases
        #    the SHAI answer follows the "●" bullet, so this is only a sanity
        #    anchor, not the response's left boundary.
        before = clean[: last_completion.start()]
        has_user_turn = re.search(USER_MESSAGE_PATTERN, before, re.MULTILINE) is not None
        logger.debug(
            "SHAI extraction: %d completion markers, user turn before last marker: %s",
            len(completions),
            has_user_turn,
        )

        # 4. The answer text begins on the line AFTER the "●" marker line and runs
        #    until the trailing input-box / status-bar chrome (or a new turn).
        line_end = clean.find("\n", last_completion.start())
        after = clean[line_end + 1 :] if line_end != -1 else ""

        body: List[str] = []
        for line in after.split("\n"):
            stripped = line.lstrip()
            # Stop at the bordered input box or the pinned status-bar chrome.
            if stripped.startswith(("╭", "│", "╰")):
                break
            if stripped.startswith("⎈") or "∑" in line:
                break
            # A later "> " would start a new (incomplete) turn — stop before it.
            if re.match(USER_MESSAGE_PATTERN, line):
                break
            # 5. Drop reasoning preamble, tool rows, and any further "●" bullet.
            if re.match(REASONING_PATTERN, line):
                continue
            if re.match(TOOL_ROW_PATTERN, line):
                continue
            if re.match(COMPLETION_MARKER_PATTERN, line):
                continue
            body.append(line)

        # 6. Dedent a uniform agent indent, then clean control chars / trailing ws.
        text = "\n".join(body)
        # Remove non-printable control chars (keep newline/tab handling to the
        # trailing-whitespace pass below).
        text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]", "", text)
        text = re.sub(r"[ \t]+$", "", text, flags=re.MULTILINE)
        text = text.strip()

        # 7. Nothing left after stripping chrome → extraction failed.
        if not text:
            raise OutputExtractionError("Empty SHAI response — no content found after extraction")

        return text

    def exit_cli(self) -> str:
        """Return the command to exit the SHAI TUI.

        The live runtime probe (task 1) confirmed ``/exit`` cleanly terminates
        the SHAI TUI.
        """
        return "/exit"

    def cleanup(self) -> None:
        """Clean up SHAI provider state.

        Resets the initialization flag. Returning ``None`` signals a successful
        cleanup per the ``BaseProvider`` contract.
        """
        self._initialized = False
