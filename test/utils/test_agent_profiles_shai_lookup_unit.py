"""Bugfix spec `shai-profile-lookup-by-id` — Task 4 unit tests.

Targeted example-based unit coverage for the native-SHAI-lookup fix in
``load_agent_profile`` (``src/cli_agent_orchestrator/utils/agent_profiles.py``).
These sit alongside the Property 1 exploration tests
(``test_agent_profiles_shai_lookup.py``) and the Property 2 preservation tests
(``test_agent_profiles_shai_lookup_preservation.py``) and must not duplicate or
modify them — they pin the concrete examples and edge cases the fix must honor:

- a native-SHAI-only id resolves by-id for BOTH ``.config.json`` and the legacy
  ``.config`` shape, with ``llm_provider`` / ``tools`` mapped into the
  ``AgentProfile`` (Req 2.1, 2.3, 3.6);
- local, built-in, and provider ids still resolve unchanged, and a
  genuinely-missing id still raises ``FileNotFoundError`` (Req 2.4, 3.1, 3.2);
- a malformed / unreadable native SHAI config is skipped and does not abort the
  lookup — it falls through to built-in / not-found (Req 3.6, 2.4);
- a same-named local and native-SHAI profile resolves the local (earlier
  scanned) copy — first-found-wins shadowing (Req 3.4).

Validates: Requirements 2.1, 2.2, 2.3, 2.4, 3.1, 3.2, 3.3, 3.4, 3.6
"""

import json
from pathlib import Path

import pytest

from cli_agent_orchestrator.models.agent_profile import AgentProfile
from cli_agent_orchestrator.utils.agent_profiles import load_agent_profile

AP = "cli_agent_orchestrator.utils.agent_profiles"
SS = "cli_agent_orchestrator.services.settings_service"
SHAI = "cli_agent_orchestrator.utils.shai_config.SHAI_USER_AGENTS_DIR"


def _write_native_shai_config(
    agents_dir: Path,
    name: str,
    *,
    description: str = "",
    system_prompt: str = "You are a native SHAI agent.",
    llm_provider: dict | None = None,
    tools: dict | None = None,
    legacy: bool = False,
    raw: str | None = None,
) -> Path:
    """Write one native SHAI agent config under ``agents_dir``.

    Modern shape ``<name>.config.json``; legacy fallback ``<name>.config``. The
    config ``name`` field is deliberately uppercased so a passing test proves
    the lookup keys off the FILENAME-derived id, not the config ``name`` field.
    Pass ``raw`` to write arbitrary (e.g. malformed) bytes verbatim.
    """
    agents_dir.mkdir(parents=True, exist_ok=True)
    suffix = ".config" if legacy else ".config.json"
    path = agents_dir / f"{name}{suffix}"
    if raw is not None:
        path.write_text(raw, encoding="utf-8")
        return path
    config: dict = {
        "name": name.upper(),  # drifts from the filename on purpose
        "description": description,
        "system_prompt": system_prompt,
    }
    if llm_provider is not None:
        config["llm_provider"] = llm_provider
    if tools is not None:
        config["tools"] = tools
    path.write_text(json.dumps(config), encoding="utf-8")
    return path


def _write_local_md(store: Path, name: str, *, description: str, body: str) -> Path:
    """Write a local-store markdown profile (`<name>.md`, frontmatter + body)."""
    store.mkdir(parents=True, exist_ok=True)
    path = store / f"{name}.md"
    path.write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n{body}",
        encoding="utf-8",
    )
    return path


@pytest.fixture(autouse=True)
def _passthrough_env(monkeypatch):
    """Pass-through env resolution so profile text is parsed verbatim."""
    monkeypatch.setattr(f"{AP}.resolve_env_vars", lambda text: text)


def _isolate_non_local_non_shai(tmp_path, monkeypatch, *, builtin_dir: Path | None = None):
    """Point provider/extra stores at nothing and the built-in store at
    ``builtin_dir`` (an empty temp dir when not given)."""
    monkeypatch.setattr(f"{SS}.get_agent_dirs", lambda: {})
    monkeypatch.setattr(f"{SS}.get_extra_agent_dirs", lambda: [])
    monkeypatch.setattr(f"{SS}.get_disabled_agent_dirs", lambda: [])
    if builtin_dir is None:
        builtin_dir = tmp_path / "empty-builtin"
        builtin_dir.mkdir()
    monkeypatch.setattr(f"{AP}.resources.files", lambda _pkg: builtin_dir)


class TestNativeShaiOnlyIdResolvesByItsListedId:
    """A native-SHAI-only id resolves by-id with provider/model/tools mapped,
    for both the modern and legacy config shapes (Req 2.1, 2.3, 3.6)."""

    def _isolate_stores(self, tmp_path, monkeypatch):
        empty_local = tmp_path / "empty-local"
        empty_local.mkdir()
        monkeypatch.setattr(f"{AP}.LOCAL_AGENT_STORE_DIR", empty_local)
        _isolate_non_local_non_shai(tmp_path, monkeypatch)

    def test_config_json_shape_maps_provider_model_and_tools(self, tmp_path, monkeypatch):
        """``<name>.config.json`` resolves with description, provider, model, and
        the ``tools.builtin`` enable list mapped onto the AgentProfile."""
        self._isolate_stores(tmp_path, monkeypatch)
        shai_dir = tmp_path / "shai-agents"
        _write_native_shai_config(
            shai_dir,
            "reviewer",
            description="Reviews code",
            llm_provider={"provider": "openai", "model": "gpt-4o"},
            tools={"builtin": ["fs_read", "fs_write"], "builtin_excluded": [], "mcp": {}},
        )
        monkeypatch.setattr(SHAI, shai_dir)

        profile = load_agent_profile("reviewer")

        assert isinstance(profile, AgentProfile)
        # filename-derived id, NOT the uppercased config "name" field.
        assert profile.name == "reviewer"
        assert profile.description == "Reviews code"
        assert profile.provider == "openai"
        assert profile.model == "gpt-4o"
        assert profile.tools == ["fs_read", "fs_write"]

    def test_legacy_config_shape_maps_provider_model(self, tmp_path, monkeypatch):
        """Legacy ``<name>.config`` (no ``.json``) resolves by its id too."""
        self._isolate_stores(tmp_path, monkeypatch)
        shai_dir = tmp_path / "shai-agents"
        _write_native_shai_config(
            shai_dir,
            "helper",
            description="A helper",
            llm_provider={"provider": "anthropic", "model": "claude-3-5"},
            legacy=True,
        )
        monkeypatch.setattr(SHAI, shai_dir)

        profile = load_agent_profile("helper")

        assert isinstance(profile, AgentProfile)
        assert profile.name == "helper"
        assert profile.description == "A helper"
        assert profile.provider == "anthropic"
        assert profile.model == "claude-3-5"

    def test_config_without_llm_provider_or_tools_leaves_them_unset(self, tmp_path, monkeypatch):
        """A minimal native config (no ``llm_provider`` / ``tools``) still
        resolves; provider/model/tools stay unset (absent)."""
        self._isolate_stores(tmp_path, monkeypatch)
        shai_dir = tmp_path / "shai-agents"
        _write_native_shai_config(shai_dir, "minimal", description="Bare agent")
        monkeypatch.setattr(SHAI, shai_dir)

        profile = load_agent_profile("minimal")

        assert isinstance(profile, AgentProfile)
        assert profile.name == "minimal"
        assert profile.description == "Bare agent"
        assert profile.provider is None
        assert profile.model is None
        assert profile.tools is None


class TestNonShaiLookupsUnchanged:
    """local / built-in / provider ids resolve unchanged and a genuinely
    missing id still raises (Req 2.4, 3.1, 3.2)."""

    def test_local_store_id_resolves_unchanged(self, tmp_path, monkeypatch):
        """A local-store id resolves to its parsed AgentProfile (Req 3.1)."""
        local = tmp_path / "local"
        _write_local_md(local, "dev", description="Local dev", body="Local prompt")
        monkeypatch.setattr(f"{AP}.LOCAL_AGENT_STORE_DIR", local)
        _isolate_non_local_non_shai(tmp_path, monkeypatch)
        # A native SHAI dir exists but holds nothing of this name.
        shai_dir = tmp_path / "shai-agents"
        _write_native_shai_config(shai_dir, "someone_else")
        monkeypatch.setattr(SHAI, shai_dir)

        profile = load_agent_profile("dev")

        assert isinstance(profile, AgentProfile)
        assert profile.name == "dev"
        assert profile.description == "Local dev"
        assert profile.system_prompt == "Local prompt"
        assert profile.provider is None

    def test_builtin_store_id_resolves_unchanged(self, tmp_path, monkeypatch):
        """A built-in-store id resolves exactly as before (Req 3.2)."""
        monkeypatch.setattr(f"{AP}.LOCAL_AGENT_STORE_DIR", tmp_path / "empty-local")
        builtin = tmp_path / "builtin"
        builtin.mkdir()
        (builtin / "reviewer.md").write_text(
            "---\nname: reviewer\ndescription: Built-in reviewer\n---\nReview prompt",
            encoding="utf-8",
        )
        _isolate_non_local_non_shai(tmp_path, monkeypatch, builtin_dir=builtin)
        shai_dir = tmp_path / "shai-agents"
        shai_dir.mkdir()  # empty native dir
        monkeypatch.setattr(SHAI, shai_dir)

        profile = load_agent_profile("reviewer")

        assert isinstance(profile, AgentProfile)
        assert profile.name == "reviewer"
        assert profile.description == "Built-in reviewer"
        assert profile.system_prompt == "Review prompt"

    def test_provider_directory_id_resolves_unchanged(self, tmp_path, monkeypatch):
        """A provider-directory id resolves exactly as before (Req 3.2)."""
        monkeypatch.setattr(f"{AP}.LOCAL_AGENT_STORE_DIR", tmp_path / "empty-local")
        provider_dir = tmp_path / "kiro-agents"
        provider_dir.mkdir()
        (provider_dir / "kagent.md").write_text(
            "---\nname: kagent\ndescription: Provider agent\n---\nProvider prompt",
            encoding="utf-8",
        )
        monkeypatch.setattr(f"{SS}.get_agent_dirs", lambda: {"kiro_cli": str(provider_dir)})
        monkeypatch.setattr(f"{SS}.get_extra_agent_dirs", lambda: [])
        monkeypatch.setattr(f"{SS}.get_disabled_agent_dirs", lambda: [])
        empty_builtin = tmp_path / "empty-builtin"
        empty_builtin.mkdir()
        monkeypatch.setattr(f"{AP}.resources.files", lambda _pkg: empty_builtin)
        shai_dir = tmp_path / "shai-agents"
        shai_dir.mkdir()
        monkeypatch.setattr(SHAI, shai_dir)

        profile = load_agent_profile("kagent")

        assert isinstance(profile, AgentProfile)
        assert profile.name == "kagent"
        assert profile.description == "Provider agent"
        assert profile.system_prompt == "Provider prompt"

    def test_genuinely_missing_id_still_raises_file_not_found(self, tmp_path, monkeypatch):
        """An id absent from every store, including the native SHAI dir, still
        raises FileNotFoundError (Req 2.4)."""
        monkeypatch.setattr(f"{AP}.LOCAL_AGENT_STORE_DIR", tmp_path / "empty-local")
        _isolate_non_local_non_shai(tmp_path, monkeypatch)
        shai_dir = tmp_path / "shai-agents"
        _write_native_shai_config(shai_dir, "present_agent")
        monkeypatch.setattr(SHAI, shai_dir)

        with pytest.raises(FileNotFoundError, match="Agent profile not found"):
            load_agent_profile("does_not_exist")


class TestMalformedNativeShaiConfigSkipped:
    """A malformed / unreadable native SHAI config is skipped and does not
    abort the lookup — it falls through to built-in / not-found (Req 3.6, 2.4)."""

    def test_malformed_native_only_id_falls_through_to_not_found(self, tmp_path, monkeypatch):
        """An id present only as a malformed (invalid-JSON) native config is NOT
        resolvable: discovery skips it, so the lookup falls through to
        FileNotFoundError rather than aborting with a parse error."""
        monkeypatch.setattr(f"{AP}.LOCAL_AGENT_STORE_DIR", tmp_path / "empty-local")
        _isolate_non_local_non_shai(tmp_path, monkeypatch)
        shai_dir = tmp_path / "shai-agents"
        _write_native_shai_config(shai_dir, "broken", raw="{ this is not valid json ]")
        monkeypatch.setattr(SHAI, shai_dir)

        with pytest.raises(FileNotFoundError, match="Agent profile not found"):
            load_agent_profile("broken")

    def test_malformed_native_config_does_not_hide_built_in_same_name(self, tmp_path, monkeypatch):
        """A malformed native config for a name that ALSO exists built-in is
        skipped, and the lookup still resolves the built-in copy (the malformed
        native entry neither resolves nor aborts the lookup)."""
        monkeypatch.setattr(f"{AP}.LOCAL_AGENT_STORE_DIR", tmp_path / "empty-local")
        builtin = tmp_path / "builtin"
        builtin.mkdir()
        (builtin / "shared.md").write_text(
            "---\nname: shared\ndescription: Built-in shared\n---\nBuilt-in prompt",
            encoding="utf-8",
        )
        _isolate_non_local_non_shai(tmp_path, monkeypatch, builtin_dir=builtin)
        shai_dir = tmp_path / "shai-agents"
        _write_native_shai_config(shai_dir, "shared", raw="not json at all")
        monkeypatch.setattr(SHAI, shai_dir)

        profile = load_agent_profile("shared")

        # The malformed native config contributed nothing; built-in wins.
        assert isinstance(profile, AgentProfile)
        assert profile.name == "shared"
        assert profile.description == "Built-in shared"
        assert profile.system_prompt == "Built-in prompt"


class TestSameNamedLocalWinsOverNativeShai:
    """A same-named local and native-SHAI profile resolves the local
    (earlier-scanned) copy — first-found-wins shadowing (Req 3.4)."""

    def test_local_copy_wins_over_same_named_native_shai(self, tmp_path, monkeypatch):
        local = tmp_path / "local"
        _write_local_md(local, "dup", description="Local dup", body="Local body")
        monkeypatch.setattr(f"{AP}.LOCAL_AGENT_STORE_DIR", local)
        _isolate_non_local_non_shai(tmp_path, monkeypatch)
        shai_dir = tmp_path / "shai-agents"
        _write_native_shai_config(
            shai_dir,
            "dup",
            description="Native dup",
            llm_provider={"provider": "openai", "model": "gpt-4o"},
        )
        monkeypatch.setattr(SHAI, shai_dir)

        profile = load_agent_profile("dup")

        # The LOCAL copy wins: local description + body, and no provider (the
        # shadowed native config named one but never reaches the result).
        assert isinstance(profile, AgentProfile)
        assert profile.name == "dup"
        assert profile.description == "Local dup"
        assert profile.system_prompt == "Local body"
        assert profile.provider is None
