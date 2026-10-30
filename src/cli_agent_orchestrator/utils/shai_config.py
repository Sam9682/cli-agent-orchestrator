"""Read-only discovery of the user's native SHAI agents, plus the CAO-managed
SHAI agent install seam.

Mirrors the ``utils/opencode_config.py`` convention: one module per provider
holding its config/agent-tree helpers, kept distinct from the provider adapter
in ``providers/shai_cli.py``.

Two directory trees, never crossed (see ``constants.py`` and the design's
isolation table):

- ``SHAI_USER_AGENTS_DIR`` (``~/.config/shai/agents``) — the user's **native**
  agents. Discovery reads these **read-only** and never writes under
  ``SHAI_USER_*``.
- ``SHAI_AGENTS_DIR`` (``~/.aws/shai_xdg/shai/agents``) — the **CAO-managed**
  tree. Installs write only here.

SHAI resolves an agent at ``$XDG_CONFIG_HOME/shai/agents/{name}.config.json``
with a legacy ``{name}.config`` fallback (per the SHAI agent-config docs). This
module enumerates both shapes for discovery.
"""

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from cli_agent_orchestrator.constants import SHAI_AGENTS_DIR, SHAI_USER_AGENTS_DIR
from cli_agent_orchestrator.utils.path_validation import (
    safe_join_under_base,
    validate_path_component,
)

logger = logging.getLogger(__name__)

#: Primary SHAI agent-config suffix (``{name}.config.json``).
_AGENT_CONFIG_SUFFIX = ".config.json"
#: Legacy SHAI agent-config suffix (``{name}.config``), still read by SHAI.
_LEGACY_AGENT_CONFIG_SUFFIX = ".config"

#: Metadata keys surfaced from a SHAI agent config, in SHAI's documented schema
#: (``name``, ``description``, ``system_prompt``, ``llm_provider``, ``tools``).
_METADATA_KEYS = ("name", "description", "system_prompt", "llm_provider", "tools")


def _agent_name_from_path(path: Path) -> str:
    """Derive the SHAI agent name from its config filename.

    ``<name>.config.json`` → ``<name>``; legacy ``<name>.config`` → ``<name>``.
    ``Path.stem`` only strips the final suffix, so ``.config.json`` needs the
    extra ``.config`` stripped.
    """
    stem = path.stem  # strips the final suffix only
    if stem.endswith(_LEGACY_AGENT_CONFIG_SUFFIX):
        # Was "<name>.config.json": stem is "<name>.config".
        return stem[: -len(_LEGACY_AGENT_CONFIG_SUFFIX)]
    return stem


def _iter_user_agent_files(agents_dir: Path) -> List[Path]:
    """Return the user's native SHAI agent-config files, newest-shape first.

    Enumerates ``*.config.json`` then legacy ``*.config`` (excluding the
    ``*.config.json`` already matched). A missing or unreadable directory yields
    an empty list — discovery never fails on absent native state.
    """
    if not agents_dir.is_dir():
        return []

    try:
        primary = sorted(agents_dir.glob(f"*{_AGENT_CONFIG_SUFFIX}"))
        # Legacy "*.config" minus the "*.config.json" the primary glob caught.
        legacy = sorted(
            p
            for p in agents_dir.glob(f"*{_LEGACY_AGENT_CONFIG_SUFFIX}")
            if not p.name.endswith(_AGENT_CONFIG_SUFFIX)
        )
    except OSError as exc:
        # Directory became unreadable between the is_dir() check and the glob.
        logger.warning("skipping unreadable SHAI agents dir %s: %s", agents_dir, exc)
        return []

    return primary + legacy


def _read_agent_metadata(path: Path) -> Optional[Dict[str, Any]]:
    """Read one SHAI agent-config file read-only, returning its metadata.

    Returns ``None`` (and logs) when the file is unreadable or not valid JSON —
    a malformed native file must not abort discovery of the others. The returned
    dict always carries a ``name`` (falling back to the filename-derived name)
    and the originating ``path`` as a string.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("skipping unreadable SHAI agent file %s: %s", path, exc)
        return None

    try:
        parsed: Any = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("skipping malformed SHAI agent file %s: %s", path, exc)
        return None

    if not isinstance(parsed, dict):
        logger.warning("skipping SHAI agent file %s: top-level value is not an object", path)
        return None

    metadata: Dict[str, Any] = {key: parsed[key] for key in _METADATA_KEYS if key in parsed}
    # SHAI keys an agent by its filename; prefer that as the canonical name so a
    # config whose "name" field drifts from its filename still imports under the
    # name SHAI actually loads it by.
    metadata["name"] = _agent_name_from_path(path)
    metadata["path"] = str(path)
    return metadata


def discover_user_agents(agents_dir: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Discover the user's native SHAI agents, read-only.

    Enumerates ``agents_dir/*.config.json`` plus the legacy ``*.config``
    fallback, reads each file read-only, and returns one metadata dict per agent
    (``name``, ``path``, and whichever of ``description`` / ``system_prompt`` /
    ``llm_provider`` / ``tools`` the file declares).

    ``agents_dir`` defaults to the module's ``SHAI_USER_AGENTS_DIR``, resolved at
    call time rather than bound once at definition. Resolving late keeps the
    documented discovery seam honest: tests (and any caller) can point discovery
    at a different native dir by patching ``shai_config.SHAI_USER_AGENTS_DIR``
    and calling with no argument, and the real default is unchanged in normal
    use.

    This is strictly read-only: a missing or unreadable directory yields ``[]``,
    an unreadable or malformed file is skipped, and nothing under
    ``SHAI_USER_*`` is ever created or modified. Results are ordered by agent
    name for stable output.

    Validates: Requirements 8.3, 8.4
    """
    if agents_dir is None:
        agents_dir = SHAI_USER_AGENTS_DIR

    agents: List[Dict[str, Any]] = []
    seen: set[str] = set()

    for path in _iter_user_agent_files(agents_dir):
        metadata = _read_agent_metadata(path)
        if metadata is None:
            continue
        name = metadata["name"]
        # A legacy "<name>.config" alongside "<name>.config.json": the primary
        # (.config.json) shape is enumerated first and wins; skip the duplicate.
        if name in seen:
            continue
        seen.add(name)
        agents.append(metadata)

    agents.sort(key=lambda a: a["name"])
    return agents


# =============================================================================
# CAO-managed SHAI profile install  —  owned by task 3.3 (do not implement here)
# =============================================================================
# The write-side helper that materializes a CAO-managed profile into
# ``SHAI_AGENTS_DIR/<profile>.config.json`` (SHAI schema: name, description,
# system_prompt, llm_provider, tools) belongs in this module but is implemented
# by task 3.3. Add it below this marker so the read-only discovery above stays
# untouched.


def _normalize_tools(tools: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Coerce a caller-supplied ``tools`` block into SHAI's documented shape.

    SHAI's agent schema expects ``tools`` to be an object with ``builtin``
    (allowed native tool names), ``builtin_excluded`` (denied native names), and
    ``mcp`` (MCP wiring). This accepts whatever the caller passes and fills in
    the missing keys so the written file always matches the schema:

    - ``None`` → ``{"builtin": [], "builtin_excluded": [], "mcp": {}}`` (no
      restriction material supplied).
    - a dict → its ``builtin`` / ``builtin_excluded`` / ``mcp`` keys are carried
      through as given, with any absent key defaulted to its empty shape.

    Callers that have a resolved CAO allowlist should pass ``allowed_tools`` to
    :func:`install_managed_agent` instead, which derives ``builtin`` /
    ``builtin_excluded`` from ``TOOL_MAPPING["shai_cli"]`` via
    :func:`_tools_from_allowlist`; this function is the pass-through for a
    fully-formed ``tools`` block.

    The returned dict is a fresh object so the caller's input is never mutated.
    """
    source = tools or {}
    return {
        "builtin": list(source.get("builtin", [])),
        "builtin_excluded": list(source.get("builtin_excluded", [])),
        "mcp": dict(source.get("mcp", {})),
    }


def _tools_from_allowlist(allowed_tools: List[str]) -> Dict[str, Any]:
    """Translate a resolved CAO allowlist into a SHAI ``tools`` block.

    SHAI tool restriction is NATIVE and install-time (Property 8): the resolved
    allowlist's mapped SHAI builtins go in ``builtin`` (the enable list), and the
    complement — every mapped builtin the allowlist does NOT grant — goes in
    ``builtin_excluded`` (the exclude list SHAI refuses at runtime).

    The two sides come from the one ``TOOL_MAPPING["shai_cli"]`` translation:
    :func:`get_allowed_tools` for ``builtin`` and :func:`get_disallowed_tools`
    for ``builtin_excluded``, so what is granted and what is blocked are exactly
    complementary over the mapped tools and cannot drift. An unrestricted ``["*"]``
    allowlist grants every mapped builtin and excludes nothing. Chrome builtins
    (skills/todo_*/memory_*) are unmapped and so appear in neither list — they
    are neither force-granted nor force-blocked.

    ``mcp`` is left empty here; MCP wiring (``@cao-mcp-server``) is layered on by
    the caller.
    """
    from cli_agent_orchestrator.utils.tool_mapping import (
        get_allowed_tools,
        get_disallowed_tools,
    )

    return {
        "builtin": get_allowed_tools("shai_cli", allowed_tools),
        "builtin_excluded": get_disallowed_tools("shai_cli", allowed_tools),
        "mcp": {},
    }


def install_managed_agent(
    profile: str,
    *,
    description: str = "",
    system_prompt: str = "",
    llm_provider: Optional[Dict[str, Any]] = None,
    tools: Optional[Dict[str, Any]] = None,
    allowed_tools: Optional[List[str]] = None,
    agents_dir: Path = SHAI_AGENTS_DIR,
) -> Path:
    """Write a CAO-managed SHAI profile into ``SHAI_AGENTS_DIR``, returning its path.

    Materializes ``agents_dir/<profile>.config.json`` in SHAI's documented agent
    schema (``name``, ``description``, ``system_prompt``, ``llm_provider``,
    ``tools``). SHAI resolves an agent at
    ``$XDG_CONFIG_HOME/shai/agents/{name}.config.json``, and the launch command
    binds ``XDG_CONFIG_HOME`` to the CAO-managed tree, so a profile written here
    is the one a CAO-managed session loads.

    Isolation and containment (Property 6): the write stays strictly inside the
    CAO-managed tree. ``profile`` is validated as a single safe path segment and
    the target is confined under ``agents_dir`` via ``safe_join_under_base``, so
    no ``profile`` value — separators, ``..``, absolute paths — can escape into
    the user's native ``SHAI_USER_*`` tree or anywhere else. ``agents_dir`` is
    created if absent. Nothing under ``SHAI_USER_*`` is ever touched.

    The file name derived from ``profile`` and the schema ``name`` field are the
    same value, matching how SHAI keys an agent by its filename (see
    ``_agent_name_from_path``).

    Tool policy (NATIVE, install-time — Property 8): pass ``allowed_tools`` to
    have ``tools.builtin`` / ``tools.builtin_excluded`` derived from the resolved
    CAO allowlist via ``TOOL_MAPPING["shai_cli"]`` (``builtin`` = the mapped
    builtins granted, ``builtin_excluded`` = the mapped complement SHAI refuses
    at runtime). When ``allowed_tools`` is given it is the source of truth and a
    ``tools`` block is ignored; otherwise a fully-formed ``tools`` block is
    accepted as given and written through (see ``_normalize_tools``), defaulting
    to the empty-restriction shape when neither is supplied.

    The write is atomic: the JSON is written to a sibling temp file and then
    ``os.replace``'d over the target, so a concurrent reader or an interrupted
    write never observes a half-written config (mirrors the OpenCode managed
    writers' temp-file + replace discipline).

    Args:
        profile: The CAO-managed profile name; becomes both the ``<profile>``
            filename stem and the schema ``name`` field. Must be a single safe
            path segment.
        description: Human-readable agent description.
        system_prompt: The CAO-composed system prompt for the agent.
        llm_provider: SHAI ``llm_provider`` block (e.g. ``provider``, ``model``,
            ``env_vars``, ``tool_method``). Defaults to an empty object.
        tools: SHAI ``tools`` block (``builtin`` / ``builtin_excluded`` / ``mcp``)
            or an equivalent partial; missing keys default to their empty shape.
            Ignored when ``allowed_tools`` is supplied.
        allowed_tools: A resolved CAO allowlist (e.g. from
            ``resolve_allowed_tools``). When given, ``tools.builtin`` /
            ``tools.builtin_excluded`` are derived from it via
            ``TOOL_MAPPING["shai_cli"]`` and ``tools`` is ignored.
        agents_dir: The CAO-managed agents directory to write into. Defaults to
            ``SHAI_AGENTS_DIR``; overridable for tests.

    Returns:
        The path of the written ``<profile>.config.json``.

    Raises:
        ValueError: If ``profile`` is not a single safe path segment or the
            resolved target would escape ``agents_dir``.

    Validates: Requirements 9.2, 9.3
    """
    # Reject any profile name that is not a single safe path segment before it
    # is ever joined — defence in depth alongside the containment check below so
    # a separator / traversal value fails loudly rather than silently writing
    # outside the CAO-managed tree (Property 6).
    validate_path_component(profile, description="SHAI profile name")

    agents_dir.mkdir(parents=True, exist_ok=True)

    # Confine the target under the CAO-managed agents dir. safe_join_under_base
    # re-validates the segment and canonicalizes + containment-checks the result,
    # guaranteeing the write lands inside agents_dir and never under SHAI_USER_*.
    target = Path(
        safe_join_under_base(
            str(agents_dir),
            f"{profile}{_AGENT_CONFIG_SUFFIX}",
            description="SHAI agent config filename",
        )
    )

    agent_config: Dict[str, Any] = {
        "name": profile,
        "description": description,
        "system_prompt": system_prompt,
        "llm_provider": dict(llm_provider or {}),
        # A resolved CAO allowlist is the source of truth when supplied; it is
        # translated once through TOOL_MAPPING["shai_cli"] so builtin and
        # builtin_excluded stay exactly complementary (Property 8). Otherwise a
        # fully-formed tools block is written through as given.
        "tools": (
            _tools_from_allowlist(allowed_tools)
            if allowed_tools is not None
            else _normalize_tools(tools)
        ),
    }

    # Atomic write: temp file in the same directory + os.replace, so a reader
    # never sees a partial file and an interrupted write leaves the prior config
    # (or nothing) intact rather than a truncated one.
    temp_path = target.with_name(f".{target.name}.tmp")
    temp_path.write_text(
        json.dumps(agent_config, indent=2) + "\n",
        encoding="utf-8",
    )
    temp_path.replace(target)

    logger.info("installed CAO-managed SHAI agent %s at %s", profile, target)
    return target
