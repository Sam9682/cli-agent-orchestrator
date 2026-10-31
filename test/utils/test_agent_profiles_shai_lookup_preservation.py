"""Bugfix spec `shai-profile-lookup-by-id` — Property 2 (Preservation).

Observation-first preservation tests for the native-SHAI-lookup fix. Every
assertion here encodes behavior OBSERVED on UNFIXED code for inputs where the
bug condition does NOT hold (``isBugCondition`` is false), so the whole module
PASSES on unfixed code and is re-run after the fix (task 3.4) to prove no
regression.

The bug condition holds only for an id whose *only* resolvable source is a
readable config under ``SHAI_USER_AGENTS_DIR``. The preserved (non-bug) domain
is therefore everything else:

- a ``local``-store id resolves and returns its parsed ``AgentProfile``;
- a ``built-in``-store id and a provider-directory id resolve and return
  exactly as before;
- a genuinely-missing id (absent from every store, including the native SHAI
  dir) still raises ``FileNotFoundError``;
- a same-named profile present in both the local store and the native SHAI
  directory still resolves the local (earlier-scanned) copy — first-found-wins
  shadowing precedence;
- ``list_agent_profiles()`` lists each native SHAI agent under its
  filename-derived id with ``source: "shai"`` (enumeration contract);
- ``PUT`` / ``DELETE`` write paths resolve the local store only and never read
  or write ``SHAI_USER_AGENTS_DIR``.

Validates: Requirements 2.4, 3.1, 3.2, 3.3, 3.4, 3.5, 3.6
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
from cli_agent_orchestrator.utils.ephemeral_names import (
    routes_to_ephemeral_store as routes_to_ephemeral,
)

AP = "cli_agent_orchestrator.utils.agent_profiles"
SS = "cli_agent_orchestrator.services.settings_service"
SHAI = "cli_agent_orchestrator.utils.shai_config.SHAI_USER_AGENTS_DIR"
PS = "cli_agent_orchestrator.services.profile_store"


def _write_local_md(store: Path, name: str, *, description: str, body: str) -> Path:
    """Write a local-store markdown profile (`<name>.md`, frontmatter + body).

    YAML-quote the name and description so generated values (leading digits,
    reserved scalars like ``=``/``yes``, colons) are parsed as the strings they
    are and never as ints, booleans, or YAML tags — the frontmatter format is
    incidental to what these tests assert (that the LOCAL copy wins), so the
    values must round-trip verbatim regardless of their characters.
    """
    store.mkdir(parents=True, exist_ok=True)
    path = store / f"{name}.md"
    path.write_text(
        f"---\nname: {json.dumps(name)}\ndescription: {json.dumps(description)}\n---\n{body}",
        encoding="utf-8",
    )
    return path


def _write_native_shai_config(
    agents_dir: Path,
    name: str,
    *,
    description: str = "",
    llm_provider: dict | None = None,
    tools: dict | None = None,
) -> Path:
    """Write one real native SHAI agent config (`<name>.config.json`)."""
    agents_dir.mkdir(parents=True, exist_ok=True)
    config: dict = {
        "name": name.upper(),  # drifts from the filename on purpose
        "description": description,
        "system_prompt": "You are a native SHAI agent.",
    }
    if llm_provider is not None:
        config["llm_provider"] = llm_provider
    if tools is not None:
        config["tools"] = tools
    path = agents_dir / f"{name}.config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return path


# A legal profile-name alphabet: no "/", "\\", or ".." (see _validate_agent_name),
# and never a reserved ephemeral name. Lowercase + digits keeps names both valid
# filenames and valid SHAI filename-derived ids.
_NAME = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz0123456789_-",
    min_size=1,
    max_size=16,
).filter(lambda s: ".." not in s and not routes_to_ephemeral(s))

# Description/text values embedded into local-store YAML frontmatter. Constrain
# to printable ASCII (letters, digits, spaces, a little punctuation) so the
# generator exercises varied-but-valid frontmatter rather than probing PyYAML's
# escape handling — the exact characters are incidental to the properties under
# test (which copy wins, that native agents enumerate). The SHAI side is written
# as JSON, which round-trips any text, so only the local-markdown path needs this.
_SAFE_TEXT = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 .,!?",
    max_size=40,
)


class TestPreservationExampleLookups:
    """Observation-first example-based preservation checks (non-bug domain)."""

    @pytest.fixture(autouse=True)
    def _passthrough_env(self, monkeypatch):
        # Pass-through env resolution so profile text is parsed verbatim.
        monkeypatch.setattr(f"{AP}.resolve_env_vars", lambda text: text)

    def _isolate_non_shai(self, tmp_path, monkeypatch, *, empty_builtin=True):
        """Point every non-local store at empty/missing locations."""
        monkeypatch.setattr(f"{SS}.get_agent_dirs", lambda: {})
        monkeypatch.setattr(f"{SS}.get_extra_agent_dirs", lambda: [])
        monkeypatch.setattr(f"{SS}.get_disabled_agent_dirs", lambda: [])
        if empty_builtin:
            empty = tmp_path / "empty-builtin"
            empty.mkdir()
            monkeypatch.setattr(f"{AP}.resources.files", lambda _pkg: empty)

    def test_local_store_id_resolves_unchanged(self, tmp_path, monkeypatch):
        """A local-store id resolves to its parsed AgentProfile (Req 3.1)."""
        local = tmp_path / "local"
        _write_local_md(local, "dev", description="Local dev", body="Local prompt")
        monkeypatch.setattr(f"{AP}.LOCAL_AGENT_STORE_DIR", local)
        self._isolate_non_shai(tmp_path, monkeypatch)

        profile = load_agent_profile("dev")

        assert isinstance(profile, AgentProfile)
        assert profile.name == "dev"
        assert profile.description == "Local dev"
        assert profile.system_prompt == "Local prompt"

    def test_builtin_store_id_resolves_unchanged(self, tmp_path, monkeypatch):
        """A built-in-store id resolves exactly as before (Req 3.2)."""
        monkeypatch.setattr(f"{AP}.LOCAL_AGENT_STORE_DIR", tmp_path / "empty-local")
        monkeypatch.setattr(f"{SS}.get_agent_dirs", lambda: {})
        monkeypatch.setattr(f"{SS}.get_extra_agent_dirs", lambda: [])
        monkeypatch.setattr(f"{SS}.get_disabled_agent_dirs", lambda: [])

        builtin = tmp_path / "builtin"
        builtin.mkdir()
        (builtin / "reviewer.md").write_text(
            "---\nname: reviewer\ndescription: Built-in reviewer\n---\nReview prompt",
            encoding="utf-8",
        )
        monkeypatch.setattr(f"{AP}.resources.files", lambda _pkg: builtin)

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
        empty = tmp_path / "empty-builtin"
        empty.mkdir()
        monkeypatch.setattr(f"{AP}.resources.files", lambda _pkg: empty)

        profile = load_agent_profile("kagent")

        assert isinstance(profile, AgentProfile)
        assert profile.name == "kagent"
        assert profile.description == "Provider agent"
        assert profile.system_prompt == "Provider prompt"

    def test_missing_id_still_raises_file_not_found(self, tmp_path, monkeypatch):
        """An id absent from every store, including the native SHAI dir, still
        raises FileNotFoundError (Req 2.4)."""
        monkeypatch.setattr(f"{AP}.LOCAL_AGENT_STORE_DIR", tmp_path / "empty-local")
        self._isolate_non_shai(tmp_path, monkeypatch)

        shai_dir = tmp_path / "shai-agents"
        _write_native_shai_config(shai_dir, "present_agent")
        monkeypatch.setattr(SHAI, shai_dir)

        with pytest.raises(FileNotFoundError, match="Agent profile not found"):
            load_agent_profile("does_not_exist")


class TestPreservationShadowing:
    """First-found-wins shadowing: local wins over a same-named native SHAI
    agent (Req 3.4). PBT over the collision domain."""

    @pytest.fixture(autouse=True)
    def _passthrough_env(self, monkeypatch):
        monkeypatch.setattr(f"{AP}.resolve_env_vars", lambda text: text)

    @settings(
        max_examples=40,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    @given(
        name=_NAME,
        local_desc=_SAFE_TEXT,
        shai_desc=st.text(max_size=40),
    )
    def test_local_wins_over_same_named_native_shai(
        self, name, local_desc, shai_desc, tmp_path, monkeypatch
    ):
        """For any colliding name, the local copy resolves — the native SHAI
        copy is shadowed by the earlier-scanned local store.

        Validates: Requirements 3.4
        """
        import uuid

        root = tmp_path / uuid.uuid4().hex
        local = root / "local"
        _write_local_md(local, name, description=local_desc, body="Local body")
        shai_dir = root / "shai-agents"
        _write_native_shai_config(
            shai_dir,
            name,
            description=shai_desc,
            llm_provider={"provider": "openai", "model": "gpt-4o"},
        )

        monkeypatch.setattr(f"{AP}.LOCAL_AGENT_STORE_DIR", local)
        monkeypatch.setattr(f"{SS}.get_agent_dirs", lambda: {})
        monkeypatch.setattr(f"{SS}.get_extra_agent_dirs", lambda: [])
        monkeypatch.setattr(f"{SS}.get_disabled_agent_dirs", lambda: [])
        empty = root / "empty-builtin"
        empty.mkdir()
        monkeypatch.setattr(f"{AP}.resources.files", lambda _pkg: empty)
        monkeypatch.setattr(SHAI, shai_dir)

        profile = load_agent_profile(name)

        # The LOCAL copy wins: its description is the frontmatter value and its
        # body is the local markdown body (never the SHAI system prompt).
        assert isinstance(profile, AgentProfile)
        assert profile.name == name
        assert profile.description == local_desc
        assert profile.system_prompt == "Local body"
        # The local markdown declares no provider, so provider stays unset even
        # though the shadowed SHAI config named one.
        assert profile.provider is None


class TestPreservationEnumeration:
    """Enumeration contract: each native SHAI agent is listed under its
    filename-derived id with source 'shai' (Req 3.3). PBT over the native-SHAI
    domain."""

    @pytest.fixture(autouse=True)
    def _passthrough_env(self, monkeypatch):
        monkeypatch.setattr(f"{AP}.resolve_env_vars", lambda text: text)

    @settings(
        max_examples=40,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    @given(
        names=st.lists(_NAME, min_size=1, max_size=5, unique=True),
        description=st.text(max_size=40),
    )
    def test_native_shai_agents_enumerated_under_filename_id_with_source_shai(
        self, names, description, tmp_path, monkeypatch
    ):
        """For any set of native SHAI configs, list_agent_profiles() surfaces
        each under its filename-derived id with source 'shai'.

        Validates: Requirements 3.3
        """
        import uuid

        root = tmp_path / uuid.uuid4().hex
        empty_local = root / "empty-local"
        empty_local.mkdir(parents=True)
        shai_dir = root / "shai-agents"
        for name in names:
            _write_native_shai_config(
                shai_dir,
                name,
                description=description,
                llm_provider={"provider": "openai", "model": "gpt-4o"},
            )

        monkeypatch.setattr(f"{AP}.LOCAL_AGENT_STORE_DIR", empty_local)
        monkeypatch.setattr(f"{SS}.get_agent_dirs", lambda: {})
        monkeypatch.setattr(f"{SS}.get_extra_agent_dirs", lambda: [])
        monkeypatch.setattr(f"{SS}.get_disabled_agent_dirs", lambda: [])
        empty_builtin = root / "empty-builtin"
        empty_builtin.mkdir()
        monkeypatch.setattr(f"{AP}.resources.files", lambda _pkg: empty_builtin)
        monkeypatch.setattr(SHAI, shai_dir)

        listed = {p["name"]: p for p in list_agent_profiles()}
        for name in names:
            assert name in listed, f"{name} missing from listing {sorted(listed)}"
            assert listed[name]["source"] == "shai"


class TestPreservationWritePathIsolation:
    """Write-path isolation: PUT (replace) / DELETE resolve the local store
    only and never read or write SHAI_USER_AGENTS_DIR (Req 3.5). PBT over
    random PUT/DELETE targets."""

    @settings(
        max_examples=40,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    @given(name=_NAME, content=st.text(max_size=60))
    def test_put_delete_never_touch_native_shai_dir(self, name, content, tmp_path, monkeypatch):
        """For any PUT/DELETE target, the write path resolves the local store
        only — the native SHAI dir is never read or written, and a native-only
        name is not writable/deletable (ProfileNotFoundError).

        Validates: Requirements 3.5
        """
        import uuid

        from cli_agent_orchestrator.services import profile_store
        from cli_agent_orchestrator.services.profile_store import (
            ProfileNotFoundError,
            delete_profile,
            replace_profile,
            write_profile,
        )

        root = tmp_path / uuid.uuid4().hex
        local = root / "local"
        local.mkdir(parents=True)
        shai_dir = root / "shai-agents"
        # A native SHAI agent of the SAME name exists only in the SHAI dir.
        _write_native_shai_config(
            shai_dir,
            name,
            description="native",
            llm_provider={"provider": "openai", "model": "gpt-4o"},
        )

        monkeypatch.setattr(f"{PS}.LOCAL_AGENT_STORE_DIR", local)
        # If the write path ever consulted the native SHAI dir, pointing it at a
        # non-existent path would surface as an error rather than a clean
        # local-store resolution.
        monkeypatch.setattr(SHAI, shai_dir)

        # Reserved ephemeral names route elsewhere; keep the target in-domain.
        if profile_store.agent_profiles.routes_to_ephemeral_store(name):
            return

        # A PUT (replace) against a native-only name must 404 (not in local
        # store) and must NOT create or touch the SHAI dir.
        with pytest.raises(ProfileNotFoundError):
            replace_profile(name, content)

        # A DELETE against a native-only name must 404 and leave the SHAI file
        # intact (never deleted from the read-only native dir).
        with pytest.raises(ProfileNotFoundError):
            delete_profile(name)

        # The native SHAI config is still present and unmodified: the write path
        # never read or wrote the native dir.
        shai_file = shai_dir / f"{name}.config.json"
        assert shai_file.exists()

        # A create/replace of the same name in the LOCAL store succeeds and
        # writes ONLY under the local store, never the native SHAI dir.
        write_profile(name, f"---\nname: {name}\ndescription: local\n---\n{content}")
        assert (local / f"{name}.md").exists()
        # The native SHAI dir gained no new file and lost none.
        assert list(shai_dir.iterdir()) == [shai_file]
