# SHAI Provider

## Overview

The `shai_cli` provider runs [OVHcloud's SHAI](https://github.com/ovh/shai)
(`ovh/shai`), a Rust terminal coding agent, as a long-lived, multi-turn agent in
a CAO tmux window. CAO launches SHAI against a CAO-owned config tree, seeds the
first turn with the selected agent profile, and reads status and responses
directly from SHAI's TUI.

SHAI is a stacked, non-alt-screen TUI: each turn appends inline rather than
repainting a full-screen view, so turns accumulate in scrollback and
`capture-pane` returns a clean, parseable frame. Status detection and response
extraction are calibrated against a live probe of `shai version 0.2.2`; a later
release can require fixture updates if its visible markers change.

## Install and authenticate

Install SHAI and authenticate once outside CAO:

```bash
# Install per the ovh/shai README, then:
shai auth          # configure an OVHcloud provider / API key
shai version
```

Authentication is stored under SHAI's native config directory
(`~/.config/shai`). A single `shai auth` carries over to CAO-managed sessions —
you do not need to re-authenticate inside the CAO config tree (see
[Config isolation](#config-isolation)). Never put access tokens or API keys in a
CAO profile or commit them to a repository.

## Quick start

```bash
cao install developer --provider shai_cli
cao-server
cao launch --agents developer --provider shai_cli
```

Pin a profile to this provider with frontmatter:

```yaml
---
name: shai_developer
description: Developer backed by SHAI
provider: shai_cli
role: developer
---

Implement the requested change and verify it.
```

## Runtime behavior

CAO waits for the shell prompt in the tmux window, then launches a command
equivalent to:

```text
env XDG_CONFIG_HOME=<CAO-managed tree> TERM=xterm-256color shai -a <agent>
```

- `XDG_CONFIG_HOME` points SHAI at the CAO-managed config tree so it loads the
  CAO-installed agent and writes session state there instead of into your
  personal SHAI files.
- `TERM=xterm-256color` keeps the TUI rendering cleanly under tmux. No telemetry
  or auto-update suppression flags are required.
- `-a <agent>` selects the CAO-managed agent profile by name.

CAO treats SHAI reaching IDLE or COMPLETED as a successful launch and waits for
that before delivering the first task. On cleanup, CAO issues SHAI's `/exit`
slash command to close the TUI and resets its own initialization state.

SHAI pins its model in the agent config JSON (`llm_provider.model`) rather than
accepting a per-launch flag — there is no `--model` CLI option. A `SHAI_MODEL`
environment variable exists if a launch-scoped override is ever needed, but the
normal path is to set the model in the agent JSON at install time. CAO's
`honors_model` is `False` for this provider.

## Config isolation

SHAI resolves its config root from `XDG_CONFIG_HOME` (falling back to
`~/.config`). CAO uses this to give SHAI a complete, CAO-owned config tree while
leaving your personal SHAI setup untouched.

| Role | Path | Access |
|------|------|--------|
| SHAI native config / auth root | `~/.config/shai` | read-only (discovery) |
| SHAI native agents | `~/.config/shai/agents` | read-only (discovery) |
| CAO-managed config root | `~/.aws/shai_xdg/shai` | read-write (CAO owns) |
| CAO-managed agents | `~/.aws/shai_xdg/shai/agents` | read-write (CAO owns) |

`XDG_CONFIG_HOME` is set to the **parent** of the `shai/` directory
(`~/.aws/shai_xdg`), so SHAI resolves `$XDG_CONFIG_HOME/shai/agents/<name>.config.json`
entirely inside the CAO-managed tree:

```text
~/.aws/shai_xdg/            ← value of XDG_CONFIG_HOME at launch
└── shai/                   ← CAO-managed config root
    └── agents/             ← CAO-managed agents
        └── <profile>.config.json
```

**Authentication falls back to the native location.** SHAI honors
`XDG_CONFIG_HOME` for agents, but when the CAO-managed tree has no
`auth.config.json` it falls back to `~/.config/shai/auth.config.json`. This means
a single `shai auth` carries over to every CAO-managed session, and CAO never has
to copy or symlink your credentials into its own tree.

### Discovery of native agents

CAO discovers your existing SHAI agents **read-only**. It enumerates
`~/.config/shai/agents/*.config.json` (and the legacy `*.config` fallback) to
surface agent names and metadata for import, opening those files read-only. CAO
never writes into `~/.config/shai` or `~/.config/shai/agents` during discovery or
import — CAO-managed profiles are written only into the CAO-owned tree above.

## Status detection

SHAI renders a stacked, non-alt-screen TUI. Each turn appends inline: a cyan `>`
user line, a dim reasoning block (`✻`), optional `→ <Tool>` rows, then a white
`●` response bullet followed by the answer. A bordered input box and a single
status-bar line are pinned at the bottom. CAO detects state from the TUI chrome:

| State | Marker |
|-------|--------|
| `IDLE` | Idle footer (`? help · / commands · esc cancel · ctrl+c quit`) present, no live spinner |
| `PROCESSING` | `Agent is working...` spinner line (leading braille glyph) present |
| `COMPLETED` | A `●` response block present, idle footer present, no live spinner |
| `WAITING_USER_ANSWER` | `🔐 Permission Required` approval dialog (or `Do you want to run this tool?`) |
| `ERROR` | Red `●` error banner — `● Error: ...` provider chrome |

A few details that matter:

- There is **no dedicated "completed" or "done in Xs" banner**. A turn is
  complete when the spinner disappears and a response bullet is present. The
  `(Ns)` duration token lives on in-flight tool rows, not on completion, so it is
  not used as a completion marker.
- `WAITING_USER_ANSWER` outranks `PROCESSING`: a tool/spinner row can still show
  above the approval dialog, so the permission prompt wins when both are present.
- The error banner is distinguished from assistant prose that merely contains the
  word "error" by its chrome — a red `●` bullet followed by a bold `Error:` — not
  by the word alone.

## Response extraction

When a turn completes, CAO extracts the final assistant response and strips
terminal control sequences and TUI chrome. Extraction anchors on the white `●`
response bullet: CAO locates the **last** `●` response block (turns accumulate
inline, so the most recent one is the current answer) and returns the text that
follows it. Reasoning lines (`✻`), tool-execution rows (`→ <Tool>` and their
indented output), the user-turn prefix (`>`), and the bottom input box and status
bar are omitted. If no response content can be extracted from a completed turn,
CAO raises an extraction error.

## Tool restrictions

SHAI enforcement is **native and install-time** (probe-confirmed). CAO writes the
resolved `allowedTools` into the agent config JSON at `cao install` time as
`tools.builtin` (the enabled built-ins) and `tools.builtin_excluded` (the denied
built-ins). SHAI removes an excluded built-in from the toolset presented to the
model at runtime — the tool is genuinely refused, not merely hidden from a menu.
A probe of a `bash`-excluded agent confirmed the model had no shell-execution
tool available and no approval dialog ever appeared.

Because enforcement is baked into the installed agent JSON, it is applied at
install time: change the profile and re-run `cao install`. There is no per-launch
tool-restriction flag — launch-time `--allowed-tools` and `role` overrides select
the installed agent but do not change its `tools`.

CAO translates its universal tool vocabulary to SHAI's native built-in names:

| CAO tool | SHAI built-in(s) |
|----------|------------------|
| `execute_bash` | `bash` |
| `fs_read` | `read` |
| `fs_write` | `write`, `edit` |
| `fs_list` | `ls`, `find` |
| `web_fetch` | `fetch` |

This matches the `shai_cli` row in
[Tool Restrictions](tool-restrictions.md#provider-enforcement) and `SECURITY.md`:
hard enforcement at install time. Use this provider for security-critical
restricted workers — an excluded built-in is physically removed from the agent's
toolset.

## Troubleshooting

### Authentication or model errors

Run `shai auth` in a normal terminal and confirm `shai` starts and completes a
turn. Because auth falls back to the native `~/.config/shai` location, a single
`shai auth` is enough — CAO does not require credentials inside its own config
tree.

### Terminal stuck as `PROCESSING`

Attach to the tmux window and inspect the bottom of the TUI. Look for the
`Agent is working...` spinner (still processing), the `🔐 Permission Required`
dialog (waiting on a decision), or the idle footer with a `●` response block
(completed). Include a scrubbed pane capture and `shai version` when reporting a
parsing regression.

### A tool was refused unexpectedly

Confirm the profile's `allowedTools` / `role` grants the tool, then re-run
`cao install` — enforcement is install-time, so a launch flag alone does not
widen it. For unrestricted access, install a profile with `allowedTools: ["*"]`.
