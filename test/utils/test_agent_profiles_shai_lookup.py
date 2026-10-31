"""Bugfix spec `shai-profile-lookup-by-id` — Property 1 (Bug Condition).

Native SHAI profiles are enumerable (``list_agent_profiles()`` surfaces each
under its filename-derived id with ``source: "shai"``) but NOT resolvable by
that same id: ``load_agent_profile(name)`` →
``_read_installed_agent_profile_source`` searches the local store, provider
dirs, extra dirs, and the built-in package store, but NEVER
``SHAI_USER_AGENTS_DIR``, so a native-SHAI-only id falls through to
``FileNotFoundError("Agent profile not found: {name}")``. The web endpoint
returns that as HTTP 404 and the frontend renders the error box.

This module encodes the DESIRED (post-fix) behavior from design Property 1:
once fixed, ``load_agent_profile`` must resolve a native-SHAI-only id via the
SHAI discovery seam and return a parsed ``AgentProfile`` under the
filename-derived id with ``provider`` / ``model`` / ``tools`` mapped from the
SHAI ``llm_provider`` / ``tools`` blocks.

ON UNFIXED CODE these assertions FAIL at the ``load_agent_profile`` call, which
raises ``FileNotFoundError`` instead of returning an ``AgentProfile`` — that
failure is the counterexample that proves the list/lookup asymmetry. After the
fix (native SHAI lookup step at provider-tier precedence) the same assertions
pass.

The seam, per design and ``shai_config.discover_user_agents``, is the module
attribute ``shai_config.SHAI_USER_AGENTS_DIR`` (resolved at call time), so each
test points discovery at a temp dir holding a real native SHAI config with no
same-named profile in any other store (``isBugCondition`` holds).

Validates: Requirements 1.1, 1.3, 2.1, 2.3
"""

import json
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from cli_agent_orchestrator.models.agent_profile import AgentProfile
from cli_agent_orchestrator.utils.agent_profiles import (
    list_agent_profiles,
    load_agent_profile,
)


def _write_native_shai_config(
    agents_dir: Path,
    name: str,
    *,
    description: str = "",
    llm_provider: dict | None = None,
    tools: dict | None = None,
    legacy: bool = False,
) -> Path:
    """Write one real native SHAI agent config under ``agents_dir``.

    Modern shape ``<name>.config.json``; legacy fallback ``<name>.config``.
    Carries SHAI's documented schema so ``discover_user_agents()`` reads real
    metadata (``name`` is deliberately uppercased here to prove the lookup keys
    off the FILENAME-derived id, not the config ``name`` field).
    """
    agents_dir.mkdir(parents=True, exist_ok=True)
    suffix = ".config" if legacy else ".config.json"
    path = agents_dir / f"{name}{suffix}"
    config: dict = {
        "name": name.upper(),  # drifts from the filename on purpose
        "description": description,
        "system_prompt": "You are a native SHAI agent.",
    }
    if llm_provider is not None:
        config["llm_provider"] = llm_provider
    if tools is not None:
        config["tools"] = tools
    path.write_text(json.dumps(config), encoding="utf-8")
    return path


class TestShaiLookupBugCondition:
    """Property 1: a native-SHAI-only id listed with source ``shai`` must be
    resolvable by that id. FAILS on unfixed code (FileNotFoundError)."""

    @pytest.fixture(autouse=True)
    def _isolate_stores(self, tmp_path, monkeypatch):
        """Point every NON-SHAI store at empty/missing locations so the only
        possible source for a profile is the native SHAI directory — this is
        the ``isBugCondition`` precondition (name resolvable ONLY under
        SHAI_USER_AGENTS_DIR).
        """
        ap = "cli_agent_orchestrator.utils.agent_profiles"
        ss = "cli_agent_orchestrator.services.settings_service"

        # Pass-through env resolution so profile text is parsed verbatim.
        monkeypatch.setattr(f"{ap}.resolve_env_vars", lambda text: text)

        # Local store: a real empty dir.
        empty_local = tmp_path / "empty-local"
        empty_local.mkdir()
        monkeypatch.setattr(f"{ap}.LOCAL_AGENT_STORE_DIR", empty_local)

        # No provider or extra dirs.
        monkeypatch.setattr(f"{ss}.get_agent_dirs", lambda: {})
        monkeypatch.setattr(f"{ss}.get_extra_agent_dirs", lambda: [])
        monkeypatch.setattr(f"{ss}.get_disabled_agent_dirs", lambda: [])

        # Built-in store: a real empty dir (so list/lookup both see nothing).
        empty_builtin = tmp_path / "empty-builtin"
        empty_builtin.mkdir()
        monkeypatch.setattr(f"{ap}.resources.files", lambda _pkg: empty_builtin)

    def _point_shai_dir(self, monkeypatch, shai_dir: Path) -> None:
        monkeypatch.setattr(
            "cli_agent_orchestrator.utils.shai_config.SHAI_USER_AGENTS_DIR", shai_dir
        )

    def test_native_shai_only_id_is_listed_but_not_loadable_by_id(
        self, tmp_path, monkeypatch
    ):
        """`agents_control_agent.config.json` is the canonical repro.

        The list promises the id (``source: "shai"``). ``load_agent_profile``
        must resolve that SAME id. On unfixed code the load raises
        ``FileNotFoundError("Agent profile not found: agents_control_agent")`` —
        the counterexample proving the asymmetry.
        """
        shai_dir = tmp_path / "shai-agents"
        _write_native_shai_config(
            shai_dir,
            "agents_control_agent",
            description="Controls agents",
            llm_provider={"provider": "openai", "model": "gpt-4o"},
            tools={"builtin": ["fs_read"], "builtin_excluded": [], "mcp": {}},
        )
        self._point_shai_dir(monkeypatch, shai_dir)

        # The list surfaces the id the lookup must honor.
        listed = {p["name"]: p for p in list_agent_profiles()}
        assert "agents_control_agent" in listed, (
            "precondition: the native SHAI agent must be enumerable under its "
            f"filename-derived id. Listed: {sorted(listed)}"
        )
        assert listed["agents_control_agent"]["source"] == "shai"

        # DESIRED behavior (Property 1). FAILS on unfixed code: load raises.
        profile = load_agent_profile("agents_control_agent")
        assert isinstance(profile, AgentProfile)
        # id is the filename-derived value the list surfaced, NOT the config
        # "name" field (which is uppercased in the fixture on purpose).
        assert profile.name == "agents_control_agent"
        assert profile.description == "Controls agents"
        assert profile.provider == "openai"
        assert profile.model == "gpt-4o"

    def test_native_shai_only_id_legacy_config_variant_resolves_by_id(
        self, tmp_path, monkeypatch
    ):
        """Legacy ``helper.config`` (no ``.json``): listed under ``helper``,
        must resolve by that id. FAILS on unfixed code (FileNotFoundError)."""
        shai_dir = tmp_path / "shai-agents"
        _write_native_shai_config(
            shai_dir,
            "helper",
            description="A helper",
            llm_provider={"provider": "anthropic", "model": "claude-3-5"},
            legacy=True,
        )
        self._point_shai_dir(monkeypatch, shai_dir)

        listed = {p["name"]: p for p in list_agent_profiles()}
        assert "helper" in listed and listed["helper"]["source"] == "shai"

        profile = load_agent_profile("helper")
        assert isinstance(profile, AgentProfile)
        assert profile.name == "helper"
        assert profile.description == "A helper"
        assert profile.provider == "anthropic"
        assert profile.model == "claude-3-5"

    def test_genuinely_missing_id_still_raises_file_not_found(
        self, tmp_path, monkeypatch
    ):
        """Edge case pinned to stay passing after the fix: an id absent from
        every store, INCLUDING the native SHAI dir, must still raise
        ``FileNotFoundError`` (so genuinely-missing ids stay 404).

        Passes on unfixed code and must keep passing post-fix (Requirement
        2.4-adjacent; design Test Case 4).
        """
        shai_dir = tmp_path / "shai-agents"
        _write_native_shai_config(shai_dir, "present_agent")
        self._point_shai_dir(monkeypatch, shai_dir)

        with pytest.raises(FileNotFoundError, match="Agent profile not found"):
            load_agent_profile("does_not_exist")

    # Scoped PBT: the bug is deterministic, so the property is scoped to the
    # native-SHAI-ONLY domain (concrete, reproducible). Generate varied
    # description / llm_provider / tools over that domain and assert each native
    # SHAI agent is resolvable by its filename-derived id with provider/model
    # mapped. FAILS on unfixed code for every example (load raises).
    _NAME = st.text(
        alphabet="abcdefghijklmnopqrstuvwxyz0123456789_-",
        min_size=1,
        max_size=16,
    ).filter(lambda s: ".." not in s)

    @settings(
        max_examples=40,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    @given(
        name=_NAME,
        description=st.text(max_size=40),
        provider=st.sampled_from(["openai", "anthropic", "ovh", "mistral"]),
        model=st.text(
            alphabet="abcdefghijklmnopqrstuvwxyz0123456789.-", min_size=1, max_size=20
        ),
        builtin=st.lists(
            st.sampled_from(["fs_read", "fs_write", "execute_bash"]),
            max_size=3,
            unique=True,
        ),
    )
    def test_any_native_shai_only_id_resolves_by_its_listed_id(
        self, name, description, provider, model, builtin, tmp_path, monkeypatch
    ):
        """Property 1 over the native-SHAI-only domain: for any valid native
        SHAI config, the filename-derived id both lists (source ``shai``) and
        loads into an ``AgentProfile`` with description/provider/model mapped.

        Validates: Requirements 2.1, 2.3

        FAILS on unfixed code: ``load_agent_profile`` raises
        ``FileNotFoundError`` for every generated native-SHAI-only id.
        """
        import uuid

        shai_dir = tmp_path / uuid.uuid4().hex / "shai-agents"
        _write_native_shai_config(
            shai_dir,
            name,
            description=description,
            llm_provider={"provider": provider, "model": model},
            tools={"builtin": builtin, "builtin_excluded": [], "mcp": {}},
        )
        self._point_shai_dir(monkeypatch, shai_dir)

        listed = {p["name"]: p for p in list_agent_profiles()}
        assert name in listed and listed[name]["source"] == "shai"

        profile = load_agent_profile(name)
        assert isinstance(profile, AgentProfile)
        assert profile.name == name
        assert profile.description == description
        assert profile.provider == provider
        assert profile.model == model
