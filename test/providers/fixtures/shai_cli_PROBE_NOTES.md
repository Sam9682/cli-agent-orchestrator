# SHAI CLI — Live Runtime Probe Notes

Task 1 of the `shai-provider-integration` spec. These notes record values confirmed
against a **live SHAI probe**, resolving the design's "Decisions Pending Runtime
Verification" items 1–7 and gating task 10 (tool-restriction enforcement).

## Probe environment

| Item | Value |
|------|-------|
| SHAI version | `shai version 0.2.2` |
| Binary | `~/.local/bin/shai` (native Rust binary) |
| Host | WSL2 Ubuntu-24.04 |
| Driver | `tmux` (120×40 pane), `tmux capture-pane -e -p` |
| Launch env | `XDG_CONFIG_HOME=~/.aws/shai_xdg TERM=xterm-256color shai -a <agent>` |
| Auth | Pre-authenticated OVHcloud; network to github/OVHcloud reachable |
| Date | captured during task-1 execution |

**All six fixtures are real, distinct capture-pane frames** (verified distinct md5),
captured with `tmux capture-pane -e -p` (raw, ANSI-preserving):

| Fixture | State | Notes |
|---------|-------|-------|
| `shai_cli_idle.txt` | IDLE | post-launch, authenticated, straight to idle |
| `shai_cli_processing.txt` | PROCESSING | `⠹ Agent is working...` spinner live |
| `shai_cli_completed.txt` | COMPLETED | single turn, `● Hello there!` |
| `shai_cli_waiting.txt` | WAITING_USER_ANSWER | `🔐 Permission Required` Bash approval dialog |
| `shai_cli_error.txt` | ERROR | `● Error: LLM error: {...model_not_found...}` |
| `shai_cli_response.txt` | COMPLETED (multi-turn, for extraction) | 2 turns + a `→ Bash` tool call; last message `hello-from-bash` |

---

## TUI chrome — confirmed values (design item 1)

SHAI renders a stacked, non-alt-screen TUI. Each turn appends inline: a cyan
`>` user line, a dim-gray `✻` reasoning block, optional `→ <Tool>` tool rows, then a
white `●` response bullet followed by the answer text. A bordered input box and a
single status-bar line are always pinned at the bottom.

### Idle footer / input hint (IDLE)
```
? help · / commands · esc cancel · ctrl+c quit
```
- Bordered input box line: `│ >  ? for help ... │` inside a `╭─ ... ─╮`/`╰─ ... ─╯` box.
- Status bar (always present, bottom line):
  `⎈ ovhcloud  ❖ <model>  ⫽ <cwd>  ☐ Manual   ∑ <tokens> ↑<in> ↓<out>`
  - `⎈` U+2388 (provider), `❖` U+2756 (model), `⫽` U+2AFD (cwd), `☐` U+2610 (mode),
    `∑` U+2211 (token total). In a git repo an extra `⌥ <branch>` (U+2325) appears.
- Separator dots are `·` U+00B7.
- **IDLE signal:** idle footer line present AND no live spinner line
  (`Agent is working...`) present.

### Processing spinner (PROCESSING)
```
 ⠹ Agent is working... (press esc to cancel)
```
- Leading glyph is an **animated braille spinner** cycling through U+2800–U+28FF
  (observed `⠹ ⠏ ⠧ ⠥ ⠦`, i.e. `⠹` U+2839 etc.). Do **not** pin to one glyph —
  match the stable text `Agent is working...`.
- Suggested `PROCESSING_PATTERN`: `Agent is working\.\.\.` (optionally require a
  leading braille glyph `[\u2800-\u28FF]`).
- Tool-execution rows during a turn also carry a spinner + duration token, e.g.
  `⠥ Bash echo hello-from-bash (9.3s)` — a `(<n>s)` duration token appears **only on
  the in-flight tool row**, not on turn completion.

### Per-turn completion marker (COMPLETED) — **IMPORTANT**
- **There is NO dedicated "completed"/"done in Xs" banner.** A turn is complete when
  the spinner line disappears and a white response bullet block is present:
  ```
  ● 
  <answer text>
  ```
  - `●` U+25CF rendered in **bright white** `\x1b[38;5;15m`. The bullet sits on its
    own line; the answer follows after a blank line.
- **COMPLETION_MARKER_PATTERN** should therefore key on the response bullet `●`
  (white) as the turn boundary, NOT a duration token. For `get_status`, COMPLETED =
  a `●` response block present + idle footer + **no** live `Agent is working...`.
- The duration token lives on tool rows (`(9.3s)`), NOT on completion — do not use it
  as a completion marker.

### Permission / approval dialog heading (WAITING_USER_ANSWER)
```
╭ 🔐 Permission Required ──...──╮
│ ╭🔧 Bash(echo hello-from-bash)─...─╮ │
│ │ env: {}                         │ │
│ Do you want to run this tool?       │
│ ❯ Allow                             │
│   Use auto mode                     │
│   Deny                              │
╰──...──╯
```
- **Heading text:** `Permission Required` (with `🔐` U+1F510 lock emoji).
- Tool box heading: `🔧` U+1F527 then `Bash(...)`.
- Options: `Allow` / `Use auto mode` / `Deny`; cursor `❯` U+276F marks the selection.
- Suggested `PERMISSION_PROMPT_PATTERN`: `Permission Required` (fallback also
  `Do you want to run this tool\?`).
- **Stale guard matters here:** while the dialog is up, a spinner/tool row
  (`⠥ Bash ... (9.3s)`) still shows ABOVE the dialog, so WAITING must outrank
  PROCESSING in the status ladder (matches the design's priority).

### Error banner (ERROR)
```
● Error: LLM error: {"error":{"code":"model_not_found","message":"..."}}
```
- Rendered in **red**: bullet `●` `\x1b[38;2;255;100;100m`, then **bold** `Error:`
  `\x1b[1m\x1b[38;2;255;150;150m`.
- Suggested `ERROR_PATTERN`: `●\s*Error:` or `\bError:\s` constrained to the provider
  error chrome. The red `●` + bold `Error:` combination distinguishes a provider error
  banner from assistant prose that merely contains the word "error" (the normal
  response `●` is white, color 15 — so colour is a strong discriminator in the raw
  ANSI frame).

### User-turn prefix (extraction boundary)
```
> <user message text>
```
- `>` U+003E in **cyan** `\x1b[38;5;6m`, a space, then the user text in light gray
  `\x1b[38;2;180;180;180m`. Appears at the **left margin** (col 0), distinct from the
  bordered input box (`│ > ... │`).
- Suggested `USER_MESSAGE_PATTERN`: `^>\s` (left-margin `>` + space) after ANSI strip.
  Combined with colour (`38;5;6`) in the raw frame it is unambiguous.

### Reasoning / "thinking" preamble (strip during extraction)
```
✻ <reasoning text, possibly multi-line>
```
- `✻` U+273B in **dim gray** `\x1b[2m\x1b[38;2;120;120;120m`. Strip these lines when
  extracting the response (keep only the `●` answer block).

### Tool-execution rows (strip during extraction)
```
→ Bash <cmd>
  → <n> chars
  <tool output lines>
```
- `→` U+2192 (green `\x1b[38;5;2m`) + bold tool name. Output indented 2 spaces, dim.

---

## Design item 2 — `XDG_CONFIG_HOME` coverage (agents vs auth)

**Confirmed, with a caveat:**
- SHAI **honors `XDG_CONFIG_HOME` for agents**: launching with
  `XDG_CONFIG_HOME=~/.aws/shai_xdg shai -a <name>` loaded the agent from
  `~/.aws/shai_xdg/shai/agents/<name>.config.json` (model + system prompt from that
  file took effect). Confirms the design's directory model & launch binding.
- **Auth does NOT require the XDG tree.** Launching with an XDG tree that had **no**
  `auth.config.json` still authenticated and ran turns successfully — SHAI falls back
  to the native `~/.config/shai/auth.config.json`. When an `auth.config.json` IS
  present under the XDG root, it is also honored (the first probe copied it in and it
  worked).
- **Implication for the provider:** CAO does **not** need to copy/symlink auth into
  the CAO-managed tree — the native-location fallback covers auth automatically, so
  the user's single `shai auth` carries over. Copying auth into the CAO tree is
  optional/harmless. The design's "fallback to copy/symlink auth" plan is therefore
  **not required** in practice (document as: native auth fallback is sufficient).

> Note: OVHcloud also exposes an **anonymous default endpoint** — a fully isolated
> HOME+XDG with no auth at all still launched to idle on a default model
> (`gpt-oss-120b`). So a hard first-run auth wall was not reproducible in this
> environment (see item on first-run auth below).

---

## Design item 3 — alt-screen / scrollback behavior

**SHAI does NOT use the terminal alternate screen.** `tmux display-message
'#{alternate_on}'` returned `0` during an active SHAI session. Consequences:
- `capture-pane -e -p` returns a clean, parseable frame; `capture-pane -S -<n>`
  returns real scrollback history (turns accumulate inline rather than being wiped).
- This is a **positive divergence from the OpenCode template** (OpenCode alt-screens
  and starves scrollback).
- **Recommendation for task 7:** SHAI is screen-safe. You can opt into
  `supports_direct_status_probe = True` and `supports_screen_detection = True` with a
  `get_status_from_screen()` (COMPLETED/IDLE are distinguishable from a snapshot:
  `●` block + idle footer + no spinner). Keep a viewport/`first_indent` fallback in
  extraction defensively, but `extraction_tail_lines` can be generous.

---

## Design item 4 — paste submission (`paste_enter_count` / `paste_submit_delay`)

**Confirmed `paste_enter_count = 1`.** Using tmux bracketed paste
(`tmux load-buffer` + `tmux paste-buffer -p`) followed by a **single** `Enter`
submitted the prompt reliably — the user turn appeared and processing began.
- **`paste_submit_delay`:** a delay of ~**0.5–0.8s** between paste and Enter worked
  in every trial. Recommend starting at `0.8` (safe); `1.0` is fine as a conservative
  default if flakiness ever appears.
- This overrides the design's placeholder default of `paste_enter_count = 2`.

---

## Design item 5 — model override flag

- The SHAI agent's model is pinned in the agent config JSON
  (`llm_provider.model`), confirmed by the probe (the status bar showed exactly the
  model named in the agent file).
- `shai --help` / `-h` produced **no output on stdout** (help is not emitted to a
  pipe); no `--model` flag is documented in `docs/configurations/agents.md`. There is
  a `SHAI_MODEL` **environment variable** (found in the binary's string table) and an
  in-TUI slash-command-driven model picker, but **no per-launch `--model` CLI flag**
  was found.
- **Recommendation:** keep `honors_model()` returning **`False`**. If a per-launch
  model is ever needed, set it via the agent JSON (`llm_provider.model`) at install
  time, or via the `SHAI_MODEL` env var in the launch command — NOT a CLI flag.

---

## Design item 6 — launch env suppression flags

- SHAI rendered cleanly in tmux with only `TERM=xterm-256color`. **No telemetry /
  auto-update / mouse-suppression env vars were required** for clean rendering
  (unlike OpenCode, which needed several `OPENCODE_DISABLE_*`).
- Keep the launch env minimal: `XDG_CONFIG_HOME=<SHAI_XDG_HOME>` +
  `TERM=xterm-256color`. (`SHAI_MODEL` is available if a model override is ever wired,
  but is not needed for normal launch.)
- Confirmed verified flags: `shai -a <agent>` (named agent, verified), `shai -p
  "<prompt>"` (headless, per docs), `/exit` (TUI exit, verified — see item 7).

---

## Design item 7 — native tool-restriction enforcement (GATES TASK 10)

**Confirmed: NATIVE, install-time enforcement — genuinely refused at runtime.**

Probe method: created two throwaway agents under the CAO-managed XDG tree —
`cao_probe_norestrict` (`builtin: [read,write,bash]`) and `cao_probe_nobash`
(`builtin: [read,write]`, `builtin_excluded: [bash]`) — and asked each to run a shell
command.

- **`cao_probe_norestrict`:** asked to `echo hello-from-bash` → SHAI raised the
  `🔐 Permission Required` dialog for `Bash(echo hello-from-bash)`, ran it on approval,
  and returned the output. (bash available.)
- **`cao_probe_nobash`:** same request → the model's own reasoning stated it has **no
  shell-execution tool** ("My available tools are limited to reading and writing
  files"), **no permission dialog ever appeared**, and no bash ran. The excluded
  builtin was **removed from the toolset presented to the model**, not merely hidden
  from a menu. (See `toolrestrict_nobash` capture content below.)

Conclusions for task 10:
- `PROVIDER_ENFORCEMENT["shai_cli"] = NATIVE` (Req 10.2).
- Enforcement is **install-time**: baked into
  `SHAI_AGENTS_DIR/<profile>.config.json` under `tools.builtin` /
  `tools.builtin_excluded`, read when the agent loads at launch. There is **no
  per-launch tool-restriction CLI flag**. ⇒ add `shai_cli` to
  `INSTALL_TIME_PROVIDERS`.
- Therefore add a `TOOL_MAPPING["shai_cli"]` translating CAO tool names → SHAI builtin
  names, and have the install step (task 3.3) write `tools.builtin` /
  `tools.builtin_excluded` from the allowlist (task 10.2 / Property 8).

### Full built-in tool-name enumeration

Canonical lowercase SHAI builtin tool names (from the live `cao_probe_nobash`
behavior, the agent-config doc, the binary's runtime string table, and the
`*ToolParams` struct names in the binary):

| SHAI builtin | Purpose | Likely CAO mapping |
|--------------|---------|--------------------|
| `read` | read file | `fs_read` |
| `write` | write file | `fs_write` |
| `edit` | edit file (in-place) | `fs_write` / edit family |
| `bash` | run shell command | `execute_bash` |
| `ls` | list directory | directory listing |
| `find` | search files/content | search/grep family |
| `fetch` | HTTP / web fetch | web fetch |
| `skills` | invoke a skill | (skills) |
| `todo_read` | read todo list | (todo) |
| `todo_write` | write todo list | (todo) |
| `memory_write` | write memory | (memory) |
| `memory_remove` | remove memory | (memory) |

Notes:
- Source evidence: runtime string cluster
  `fetch ls write todo_read todo_write skills memory_write memory_remove`; `*ToolParams`
  structs `Bash, Edit, Fetch, Find, Ls, Read, Write, Skill, TodoWrite, Memory*`; doc
  examples `read, write, bash`. An `Unknown builtin tool:` error string confirms the
  names are validated against this fixed registry.
- `grep`/`glob` tokens appear in the binary only as tree-sitter/highlight query
  strings, NOT as builtin tool names — the search tool is `find`.
- MCP tools live under `tools.mcp` (separate from `builtin`).

### Confirmed agent-config schema (write target for install, task 3.3)

Real example on disk (`~/.config/shai/agents/*.config.json`) and per
`docs/configurations/agents.md`:
```jsonc
{
  "name": "<profile>",
  "description": "...",
  "llm_provider": { "provider": "ovhcloud", "env_vars": {}, "model": "<model>", "tool_method": "FunctionCall" },
  "tools": { "builtin": ["read","write","bash"], "builtin_excluded": [], "mcp": {} },
  "system_prompt": "...",
  "max_tokens": 8192,
  "temperature": 0.3,
  "compaction": { ... },
  "verification": { ... }
}
```
- Agent path resolution: `$XDG_CONFIG_HOME/shai/agents/{name}.config.json`, with legacy
  `{name}.config` (no `.json`) read as fallback.

---

## Other confirmed facts (useful for later tasks)

- **Exit command:** `/exit` cleanly terminates the TUI and returns to the shell prompt
  (Req 6.1 verified). A slash-command autocomplete popup (`╭Commands─╮` listing
  `/exit`, `/tc`, `/temp`, `/tokens`, `/theme`, ...) appears while typing `/`.
- **Single Enter** submits a typed prompt too (not just paste).
- **Turns accumulate inline** (no per-turn screen clear) — extraction must find the
  **last** `●` response block and the **last** `>` user line before it.
- **First-run auth screen:** could **not** be reproduced in this environment because
  OVHcloud serves an anonymous default endpoint (isolated HOME+XDG still launched to
  idle on `gpt-oss-120b`). The design's expectation — that a no-provider first run
  surfaces as `WAITING_USER_ANSWER` and `_handle_startup_prompts` dismisses/settles it
  — remains **UNVERIFIED (could not be forced here)**. Auth is configured via the
  `shai auth` CLI subcommand, not an in-TUI `/login` slash command. Recommend keeping
  `_handle_startup_prompts` tolerant: treat a stable idle footer as ready, and treat a
  non-self-clearing dialog as `WAITING_USER_ANSWER`.

---

## Status vs design "Decisions Pending Runtime Verification"

| # | Item | Status |
|---|------|--------|
| 1 | TUI chrome regex values | **CONFIRMED** (idle footer, spinner text, `●` completion/response bullet, `Permission Required`, `● Error:`, `^>` user prefix) |
| 2 | `XDG_CONFIG_HOME` coverage | **CONFIRMED** — agents honored; auth falls back to native `~/.config/shai` (no copy needed) |
| 3 | Alt-screen / scrollback | **CONFIRMED** — no alt-screen; screen-safe; can opt into screen detection |
| 4 | Paste submission | **CONFIRMED** — `paste_enter_count = 1`, `paste_submit_delay ≈ 0.8s` |
| 5 | `--model` flag | **CONFIRMED absent** — keep `honors_model() = False`; model via agent JSON / `SHAI_MODEL` env |
| 6 | Launch env suppression flags | **CONFIRMED** — none needed beyond `TERM=xterm-256color` |
| 7 | Native tool enforcement | **CONFIRMED** — NATIVE, install-time; full builtin list enumerated; gates task 10 → `NATIVE` + `INSTALL_TIME_PROVIDERS` + `TOOL_MAPPING["shai_cli"]` |

## Tool-restriction proof capture (`cao_probe_nobash`)

Verbatim pane excerpt proving the excluded `bash` builtin is refused at runtime (no
permission dialog; model has no shell tool):
```
> Run the shell command echo test123 using bash right now.
✻ The user is asking me to run a shell command. However, I don't have access to a shell command execution tool in my
available functions. I only have read and write functions for file operations.
I should let the user know that I cannot execute shell commands with the tools available to me.
●
I don't have access to a shell command execution tool. My available tools are limited to reading and writing files. I
cannot run bash commands directly.
```
