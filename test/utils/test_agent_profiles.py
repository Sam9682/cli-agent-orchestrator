"""Tests for agent profile utilities."""

import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from cli_agent_orchestrator.models.agent_profile import AgentProfile
from cli_agent_orchestrator.utils.agent_profiles import (
    load_agent_profile,
    parse_agent_profile_text,
    resolve_provider,
)
from cli_agent_orchestrator.utils.ephemeral_names import (
    routes_to_ephemeral_store as routes_to_ephemeral,
)


class TestLoadAgentProfile:
    """Tests for load_agent_profile function."""

    @pytest.fixture(autouse=True)
    def no_managed_env(self, monkeypatch):
        monkeypatch.setattr(
            "cli_agent_orchestrator.utils.agent_profiles.resolve_env_vars", lambda text: text
        )

    def test_load_agent_profile_from_local_store(self, tmp_path, monkeypatch):
        """Test loading agent profile from the local store."""
        local_store = tmp_path / "agent-store"
        local_store.mkdir()
        (local_store / "test-agent.md").write_text(
            "---\nname: test-agent\ndescription: Test agent\n---\nSystem prompt content"
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.utils.agent_profiles.LOCAL_AGENT_STORE_DIR", local_store
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.settings_service.get_agent_dirs", lambda: {}
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.settings_service.get_extra_agent_dirs", lambda: []
        )

        result = load_agent_profile("test-agent")

        assert result.name == "test-agent"
        assert result.description == "Test agent"
        assert result.system_prompt == "System prompt content"

    def test_load_agent_profile_from_builtin_store(self, tmp_path, monkeypatch):
        """Test loading agent profile from the built-in store when local store is empty."""
        # Point the local store at an empty directory so we fall through to built-in.
        monkeypatch.setattr(
            "cli_agent_orchestrator.utils.agent_profiles.LOCAL_AGENT_STORE_DIR",
            tmp_path / "empty-local",
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.settings_service.get_agent_dirs", lambda: {}
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.settings_service.get_extra_agent_dirs", lambda: []
        )

        # Fake built-in store backed by a real on-disk directory so `is_file()`
        # and `read_text()` behave like Traversable would.
        builtin_store = tmp_path / "builtin-store"
        builtin_store.mkdir()
        (builtin_store / "builtin-agent.md").write_text(
            "---\nname: builtin-agent\ndescription: Builtin agent\n---\nBuiltin prompt"
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.utils.agent_profiles.resources.files",
            lambda _pkg: builtin_store,
        )

        result = load_agent_profile("builtin-agent")

        assert result.name == "builtin-agent"
        assert result.description == "Builtin agent"
        assert result.system_prompt == "Builtin prompt"

    def test_load_agent_profile_not_found(self, tmp_path, monkeypatch):
        """Missing profile in every store should raise FileNotFoundError."""
        monkeypatch.setattr(
            "cli_agent_orchestrator.utils.agent_profiles.LOCAL_AGENT_STORE_DIR",
            tmp_path / "empty-local",
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.settings_service.get_agent_dirs", lambda: {}
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.settings_service.get_extra_agent_dirs", lambda: []
        )
        empty_builtin = tmp_path / "empty-builtin"
        empty_builtin.mkdir()
        monkeypatch.setattr(
            "cli_agent_orchestrator.utils.agent_profiles.resources.files",
            lambda _pkg: empty_builtin,
        )

        with pytest.raises(FileNotFoundError, match="Agent profile not found"):
            load_agent_profile("nonexistent")

    def test_load_agent_profile_exception_handling(self, tmp_path, monkeypatch):
        """Test exception handling in load_agent_profile wraps unexpected errors."""
        local_store = tmp_path / "agent-store"
        local_store.mkdir()
        profile = local_store / "test-agent.md"
        profile.write_text("---\nname: test-agent\ndescription: Test\n---\nprompt")

        monkeypatch.setattr(
            "cli_agent_orchestrator.utils.agent_profiles.LOCAL_AGENT_STORE_DIR", local_store
        )
        # Inject a failure during parsing to exercise the wrapping RuntimeError branch.
        monkeypatch.setattr(
            "cli_agent_orchestrator.utils.agent_profiles.parse_agent_profile_text",
            lambda *a, **kw: (_ for _ in ()).throw(Exception("parse error")),
        )

        with pytest.raises(RuntimeError, match="Failed to load agent profile"):
            load_agent_profile("test-agent")


class TestResolveProvider:
    """Tests for resolve_provider function."""

    @patch("cli_agent_orchestrator.utils.agent_profiles.load_agent_profile")
    def test_returns_profile_provider_when_valid(self, mock_load):
        """Profile with a valid provider key should override the fallback."""
        mock_load.return_value = AgentProfile(
            name="developer", description="Dev agent", provider="claude_code"
        )

        result = resolve_provider("developer", fallback_provider="kiro_cli")

        assert result == "claude_code"
        mock_load.assert_called_once_with("developer")

    @patch("cli_agent_orchestrator.utils.agent_profiles.load_agent_profile")
    def test_returns_fallback_when_no_provider_key(self, mock_load):
        """Profile without a provider key should fall back to the caller's provider."""
        mock_load.return_value = AgentProfile(name="reviewer", description="Reviewer agent")

        result = resolve_provider("reviewer", fallback_provider="kiro_cli")

        assert result == "kiro_cli"

    @patch("cli_agent_orchestrator.utils.agent_profiles.load_agent_profile")
    def test_returns_fallback_when_provider_is_invalid(self, mock_load, caplog):
        """Profile with an invalid provider value should fall back and log a warning."""
        mock_load.return_value = AgentProfile(
            name="developer", description="Dev agent", provider="claud_code"
        )

        with caplog.at_level(logging.WARNING):
            result = resolve_provider("developer", fallback_provider="kiro_cli")

        assert result == "kiro_cli"
        assert "invalid provider" in caplog.text.lower()
        assert "claud_code" in caplog.text

    @patch("cli_agent_orchestrator.utils.agent_profiles.load_agent_profile")
    def test_returns_fallback_when_profile_not_found(self, mock_load):
        """Missing profile should fall back without raising."""
        mock_load.side_effect = RuntimeError("Failed to load agent profile 'ghost'")

        result = resolve_provider("ghost", fallback_provider="kiro_cli")

        assert result == "kiro_cli"

    @patch("cli_agent_orchestrator.utils.agent_profiles.load_agent_profile")
    def test_all_valid_provider_types_accepted(self, mock_load):
        """Each ProviderType enum value should be accepted as a valid provider."""
        from cli_agent_orchestrator.constants import PROVIDERS

        for provider_value in PROVIDERS:
            mock_load.return_value = AgentProfile(
                name="agent", description="test", provider=provider_value
            )
            result = resolve_provider("agent", fallback_provider="kiro_cli")
            assert result == provider_value

    @patch("cli_agent_orchestrator.utils.agent_profiles.load_agent_profile")
    def test_returns_fallback_when_provider_is_empty_string(self, mock_load):
        """Empty string provider should be treated as absent and fall back."""
        mock_load.return_value = AgentProfile(
            name="developer", description="Dev agent", provider=""
        )

        result = resolve_provider("developer", fallback_provider="kiro_cli")

        assert result == "kiro_cli"


class TestListAgentProfiles:
    """Tests for list_agent_profiles function."""

    @patch("cli_agent_orchestrator.services.settings_service.get_extra_agent_dirs", return_value=[])
    @patch("cli_agent_orchestrator.services.settings_service.get_agent_dirs")
    @patch("cli_agent_orchestrator.utils.agent_profiles._scan_directory")
    @patch("cli_agent_orchestrator.utils.agent_profiles.LOCAL_AGENT_STORE_DIR")
    @patch("cli_agent_orchestrator.utils.agent_profiles.resources")
    def test_list_agent_profiles_discovers_from_multiple_directories(
        self, mock_resources, mock_local_dir, mock_scan, mock_get_agent_dirs, mock_get_extra_dirs
    ):
        """Test that list_agent_profiles discovers profiles from multiple directories."""
        from cli_agent_orchestrator.utils.agent_profiles import list_agent_profiles

        # Setup built-in store with one profile
        mock_builtin_file = MagicMock()
        mock_builtin_file.name = "builtin-agent.md"
        mock_builtin_file.read_text.return_value = "---\ndescription: A built-in agent\n---\nPrompt"
        mock_agent_store = MagicMock()
        mock_agent_store.iterdir.return_value = [mock_builtin_file]
        mock_resources.files.return_value = mock_agent_store

        # Setup local store dir
        mock_local_dir.exists.return_value = True
        mock_local_dir.resolve.return_value = Path(
            "/home/user/.aws/cli-agent-orchestrator/agent-store"
        )

        # Setup provider dirs: kiro_cli dir with a third profile
        mock_get_agent_dirs.return_value = {
            "kiro_cli": "/home/user/.kiro/agents",
        }

        def fake_scan(
            directory, source_label, profiles, name_sources=None, dir_profiles_loadable=True
        ):
            if source_label == "local":
                profiles["local-agent"] = {
                    "name": "local-agent",
                    "description": "A local agent",
                    "source": "local",
                }
            elif source_label == "kiro":
                profiles["kiro-agent"] = {
                    "name": "kiro-agent",
                    "description": "A Kiro agent",
                    "source": "kiro",
                }

        mock_scan.side_effect = fake_scan

        result = list_agent_profiles()

        # Should have built-in + local + kiro profiles
        names = [p["name"] for p in result]
        assert "builtin-agent" in names
        assert "local-agent" in names
        assert "kiro-agent" in names

    @patch("cli_agent_orchestrator.services.settings_service.get_extra_agent_dirs", return_value=[])
    @patch("cli_agent_orchestrator.services.settings_service.get_agent_dirs", return_value={})
    @patch("cli_agent_orchestrator.utils.agent_profiles._scan_directory")
    @patch("cli_agent_orchestrator.utils.agent_profiles.LOCAL_AGENT_STORE_DIR")
    @patch("cli_agent_orchestrator.utils.agent_profiles.resources")
    def test_list_agent_profiles_deduplicates_profiles_with_same_name(
        self, mock_resources, mock_local_dir, mock_scan, mock_get_agent_dirs, mock_get_extra_dirs
    ):
        """Same-named profile in built-in + on-disk: the on-disk copy wins, because
        built-in is scanned last to match the loader (_read_agent_profile_source)."""
        from cli_agent_orchestrator.utils.agent_profiles import list_agent_profiles

        # Built-in store has "developer" profile
        mock_builtin_file = MagicMock()
        mock_builtin_file.name = "developer.md"
        mock_builtin_file.read_text.return_value = (
            "---\ndescription: Built-in developer\n---\nPrompt"
        )
        mock_agent_store = MagicMock()
        mock_agent_store.iterdir.return_value = [mock_builtin_file]
        mock_resources.files.return_value = mock_agent_store

        # Local store also has "developer" profile — this WINS; built-in is scanned last.
        mock_local_dir.exists.return_value = True
        mock_local_dir.resolve.return_value = Path(
            "/home/user/.aws/cli-agent-orchestrator/agent-store"
        )

        def fake_scan(
            directory, source_label, profiles, name_sources=None, dir_profiles_loadable=True
        ):
            if source_label == "local":
                # Mirror _scan_directory: record the source AND keep first-found.
                if name_sources is not None:
                    name_sources.setdefault("developer", []).append("local")
                if "developer" not in profiles:
                    profiles["developer"] = {
                        "name": "developer",
                        "description": "Local developer",
                        "source": "local",
                    }

        mock_scan.side_effect = fake_scan

        result = list_agent_profiles()

        # Exactly one "developer", and it's the on-disk (local) copy — matches what loads.
        developer_profiles = [p for p in result if p["name"] == "developer"]
        assert len(developer_profiles) == 1
        assert developer_profiles[0]["source"] == "local"
        # built-in is scanned last, so it lands in duplicated_in — not as the winner.
        assert developer_profiles[0]["duplicated_in"] == ["built-in"]

    @patch(
        "cli_agent_orchestrator.utils.shai_config.SHAI_USER_AGENTS_DIR",
        Path("/nonexistent/shai/agents"),
    )
    @patch("cli_agent_orchestrator.services.settings_service.get_extra_agent_dirs", return_value=[])
    @patch("cli_agent_orchestrator.services.settings_service.get_agent_dirs", return_value={})
    @patch("cli_agent_orchestrator.utils.agent_profiles._scan_directory")
    @patch("cli_agent_orchestrator.utils.agent_profiles.LOCAL_AGENT_STORE_DIR")
    @patch("cli_agent_orchestrator.utils.agent_profiles.resources")
    def test_list_agent_profiles_includes_builtin_profiles(
        self, mock_resources, mock_local_dir, mock_scan, mock_get_agent_dirs, mock_get_extra_dirs
    ):
        """Test that built-in profiles are included and marked with source 'built-in'."""
        from cli_agent_orchestrator.utils.agent_profiles import list_agent_profiles

        # Setup two built-in profiles
        mock_file1 = MagicMock()
        mock_file1.name = "developer.md"
        mock_file1.read_text.return_value = "---\ndescription: Developer agent\n---\nPrompt"
        mock_file2 = MagicMock()
        mock_file2.name = "reviewer.md"
        mock_file2.read_text.return_value = "---\ndescription: Reviewer agent\n---\nPrompt"
        mock_agent_store = MagicMock()
        mock_agent_store.iterdir.return_value = [mock_file1, mock_file2]
        mock_resources.files.return_value = mock_agent_store

        # No local, provider, or extra dirs
        mock_local_dir.exists.return_value = False

        result = list_agent_profiles()

        # Verify built-in profiles are present with correct source
        names = {p["name"] for p in result}
        assert "developer" in names
        assert "reviewer" in names
        for p in result:
            assert p["source"] == "built-in"

    @patch("cli_agent_orchestrator.services.settings_service.get_extra_agent_dirs")
    @patch("cli_agent_orchestrator.services.settings_service.get_agent_dirs")
    @patch("cli_agent_orchestrator.utils.agent_profiles._scan_directory")
    @patch("cli_agent_orchestrator.utils.agent_profiles.LOCAL_AGENT_STORE_DIR")
    @patch("cli_agent_orchestrator.utils.agent_profiles.resources")
    def test_list_agent_profiles_handles_nonexistent_directories(
        self, mock_resources, mock_local_dir, mock_scan, mock_get_agent_dirs, mock_get_extra_dirs
    ):
        """Test that nonexistent directories are handled gracefully without errors."""
        from cli_agent_orchestrator.utils.agent_profiles import list_agent_profiles

        # No built-in profiles (simulate error)
        mock_resources.files.side_effect = Exception("Package not found")

        # Local store does not exist
        mock_local_dir.exists.return_value = False

        # Provider dirs point to nonexistent paths
        mock_get_agent_dirs.return_value = {
            "kiro_cli": "/nonexistent/kiro/agents",
            "cao_installed": "/nonexistent/cao/agents",
        }
        mock_get_extra_dirs.return_value = ["/nonexistent/extra/dir"]

        result = list_agent_profiles()

        # Should return empty list without raising any exceptions
        assert isinstance(result, list)

    @patch("cli_agent_orchestrator.services.settings_service.get_extra_agent_dirs")
    @patch("cli_agent_orchestrator.services.settings_service.get_agent_dirs", return_value={})
    @patch("cli_agent_orchestrator.utils.agent_profiles._scan_directory")
    @patch("cli_agent_orchestrator.utils.agent_profiles.LOCAL_AGENT_STORE_DIR")
    @patch("cli_agent_orchestrator.utils.agent_profiles.resources")
    def test_list_agent_profiles_scans_extra_dirs_from_settings(
        self, mock_resources, mock_local_dir, mock_scan, mock_get_agent_dirs, mock_get_extra_dirs
    ):
        """Test that extra_dirs from settings service are scanned for profiles."""
        from cli_agent_orchestrator.utils.agent_profiles import list_agent_profiles

        # No built-in profiles
        mock_agent_store = MagicMock()
        mock_agent_store.iterdir.return_value = []
        mock_resources.files.return_value = mock_agent_store

        # No local store
        mock_local_dir.exists.return_value = False

        # Extra dirs from settings
        mock_get_extra_dirs.return_value = [
            "/custom/agents/dir1",
            "/custom/agents/dir2",
        ]

        scan_calls = []

        def track_scan(
            directory, source_label, profiles, name_sources=None, dir_profiles_loadable=True
        ):
            scan_calls.append((str(directory), source_label))
            if str(directory) == "/custom/agents/dir1":
                profiles["custom-agent1"] = {
                    "name": "custom-agent1",
                    "description": "Custom agent 1",
                    "source": "custom",
                }
            elif str(directory) == "/custom/agents/dir2":
                profiles["custom-agent2"] = {
                    "name": "custom-agent2",
                    "description": "Custom agent 2",
                    "source": "custom",
                }

        mock_scan.side_effect = track_scan

        result = list_agent_profiles()

        # Verify extra dirs were scanned with "custom" source label
        extra_scans = [(d, l) for d, l in scan_calls if l == "custom"]
        assert len(extra_scans) == 2
        assert ("/custom/agents/dir1", "custom") in extra_scans
        assert ("/custom/agents/dir2", "custom") in extra_scans

        # Verify profiles from extra dirs are returned
        names = [p["name"] for p in result]
        assert "custom-agent1" in names
        assert "custom-agent2" in names

    @patch(
        "cli_agent_orchestrator.utils.shai_config.SHAI_USER_AGENTS_DIR",
        Path("/nonexistent/shai/agents"),
    )
    @patch("cli_agent_orchestrator.services.settings_service.get_extra_agent_dirs", return_value=[])
    @patch("cli_agent_orchestrator.services.settings_service.get_agent_dirs", return_value={})
    @patch("cli_agent_orchestrator.utils.agent_profiles._scan_directory")
    @patch("cli_agent_orchestrator.utils.agent_profiles.LOCAL_AGENT_STORE_DIR")
    @patch("cli_agent_orchestrator.utils.agent_profiles.resources")
    def test_list_agent_profiles_returns_sorted_by_name(
        self, mock_resources, mock_local_dir, mock_scan, mock_get_agent_dirs, mock_get_extra_dirs
    ):
        """Test that returned profiles are sorted alphabetically by name."""
        from cli_agent_orchestrator.utils.agent_profiles import list_agent_profiles

        # Built-in profiles in non-alphabetical order
        mock_file_z = MagicMock()
        mock_file_z.name = "zebra.md"
        mock_file_z.read_text.return_value = "---\ndescription: Zebra\n---\nPrompt"
        mock_file_a = MagicMock()
        mock_file_a.name = "alpha.md"
        mock_file_a.read_text.return_value = "---\ndescription: Alpha\n---\nPrompt"
        mock_file_m = MagicMock()
        mock_file_m.name = "middle.md"
        mock_file_m.read_text.return_value = "---\ndescription: Middle\n---\nPrompt"
        mock_agent_store = MagicMock()
        mock_agent_store.iterdir.return_value = [mock_file_z, mock_file_a, mock_file_m]
        mock_resources.files.return_value = mock_agent_store

        mock_local_dir.exists.return_value = False

        result = list_agent_profiles()

        names = [p["name"] for p in result]
        assert names == sorted(names)
        assert names == ["alpha", "middle", "zebra"]

    # ── SHAI discovery-fix: Property 1 (Bug Condition) — agent surface ────
    #
    # Bug condition per design: isBugCondition(AGENT_LISTING) holds when a valid
    # native SHAI agent exists under SHAI_USER_AGENTS_DIR but its discovered
    # name is absent from the agent listing. The seam is the native dir itself:
    # these tests populate a fake SHAI_USER_AGENTS_DIR (the default arg of
    # discover_user_agents) with real *.config.json / legacy *.config files and
    # assert the agent is surfaced.
    #
    # On UNFIXED code discover_user_agents() has no caller and
    # SHAI_USER_AGENTS_DIR is never scanned, so the agent is absent — these
    # assertions FAIL, and that failure is the counterexample that proves the
    # bug. After Change 2 folds discover_user_agents() into
    # _list_installed_profiles(), the same assertions pass with source == "shai"
    # and loadable is False.

    @staticmethod
    def _write_native_shai_agent(agents_dir: Path, name: str, *, legacy: bool = False) -> Path:
        """Write a valid native SHAI agent config into `agents_dir`.

        Modern shape: `<name>.config.json`; legacy fallback: `<name>.config`.
        Both carry SHAI's documented schema (name/description/system_prompt/
        llm_provider/tools) so discover_user_agents() reads real metadata.
        """
        import json

        agents_dir.mkdir(parents=True, exist_ok=True)
        suffix = ".config" if legacy else ".config.json"
        path = agents_dir / f"{name}{suffix}"
        path.write_text(
            json.dumps(
                {
                    "name": name,
                    "description": f"Native SHAI agent {name}",
                    "system_prompt": "You are a SHAI agent.",
                    "llm_provider": {"type": "openai"},
                    "tools": {"builtin": [], "builtin_excluded": [], "mcp": {}},
                }
            ),
            encoding="utf-8",
        )
        return path

    @patch("cli_agent_orchestrator.services.settings_service.get_extra_agent_dirs", return_value=[])
    @patch("cli_agent_orchestrator.services.settings_service.get_agent_dirs", return_value={})
    @patch("cli_agent_orchestrator.utils.agent_profiles._scan_directory")
    @patch("cli_agent_orchestrator.utils.agent_profiles.LOCAL_AGENT_STORE_DIR")
    @patch("cli_agent_orchestrator.utils.agent_profiles.resources")
    def test_list_agent_profiles_surfaces_native_shai_agent_modern_shape(
        self,
        mock_resources,
        mock_local_dir,
        mock_scan,
        mock_get_agent_dirs,
        mock_get_extra_dirs,
        tmp_path,
        monkeypatch,
    ):
        """A native `AGENTS_CONTROL_AGENT.config.json` under SHAI_USER_AGENTS_DIR
        is surfaced by list_agent_profiles() with source 'shai', loadable False.

        FAILS on unfixed code: SHAI_USER_AGENTS_DIR is never scanned and
        discover_user_agents() is never called, so AGENTS_CONTROL_AGENT is
        absent. Counterexample: native file exists, agent not listed.
        """
        from cli_agent_orchestrator.utils.agent_profiles import list_agent_profiles

        # Silence the non-SHAI sources so the listing reflects only SHAI here.
        mock_agent_store = MagicMock()
        mock_agent_store.iterdir.return_value = []
        mock_resources.files.return_value = mock_agent_store
        mock_local_dir.exists.return_value = False

        # Fake native SHAI dir — the default arg of discover_user_agents().
        shai_dir = tmp_path / "shai-agents"
        self._write_native_shai_agent(shai_dir, "AGENTS_CONTROL_AGENT")
        monkeypatch.setattr(
            "cli_agent_orchestrator.utils.shai_config.SHAI_USER_AGENTS_DIR", shai_dir
        )

        result = list_agent_profiles()

        by_name = {p["name"]: p for p in result}
        assert "AGENTS_CONTROL_AGENT" in by_name, (
            "BUG (expected on unfixed code): a valid native SHAI agent exists at "
            f"{shai_dir}/AGENTS_CONTROL_AGENT.config.json but is absent from the "
            f"agent listing. Returned names: {sorted(by_name)}"
        )
        entry = by_name["AGENTS_CONTROL_AGENT"]
        assert entry["source"] == "shai"
        assert entry["loadable"] is False

    @patch("cli_agent_orchestrator.services.settings_service.get_extra_agent_dirs", return_value=[])
    @patch("cli_agent_orchestrator.services.settings_service.get_agent_dirs", return_value={})
    @patch("cli_agent_orchestrator.utils.agent_profiles._scan_directory")
    @patch("cli_agent_orchestrator.utils.agent_profiles.LOCAL_AGENT_STORE_DIR")
    @patch("cli_agent_orchestrator.utils.agent_profiles.resources")
    def test_list_agent_profiles_surfaces_native_shai_agent_legacy_shape(
        self,
        mock_resources,
        mock_local_dir,
        mock_scan,
        mock_get_agent_dirs,
        mock_get_extra_dirs,
        tmp_path,
        monkeypatch,
    ):
        """A legacy-shape native `foo.config` under SHAI_USER_AGENTS_DIR is
        surfaced by list_agent_profiles() with source 'shai', loadable False.

        FAILS on unfixed code: the native dir is never scanned, so `foo` is
        absent. Counterexample: legacy native file exists, agent not listed.
        """
        from cli_agent_orchestrator.utils.agent_profiles import list_agent_profiles

        mock_agent_store = MagicMock()
        mock_agent_store.iterdir.return_value = []
        mock_resources.files.return_value = mock_agent_store
        mock_local_dir.exists.return_value = False

        shai_dir = tmp_path / "shai-agents"
        self._write_native_shai_agent(shai_dir, "foo", legacy=True)
        monkeypatch.setattr(
            "cli_agent_orchestrator.utils.shai_config.SHAI_USER_AGENTS_DIR", shai_dir
        )

        result = list_agent_profiles()

        by_name = {p["name"]: p for p in result}
        assert "foo" in by_name, (
            "BUG (expected on unfixed code): a valid legacy native SHAI agent "
            f"exists at {shai_dir}/foo.config but is absent from the agent "
            f"listing. Returned names: {sorted(by_name)}"
        )
        entry = by_name["foo"]
        assert entry["source"] == "shai"
        assert entry["loadable"] is False


class TestListAgentProfilesPreservation:
    """SHAI discovery-fix: Property 2 (Preservation) — agent surface.

    Preservation per design: for any discovery query where the bug condition
    does NOT hold, the fixed code produces the same result as the original for
    every NON-SHAI entry — same names, same ``source``/``loadable``, same
    dedup/precedence (first-found wins; built-in scanned last) and
    ``duplicated_in`` conflict flagging, same alphabetical sort — and an absent,
    empty, or unreadable ``SHAI_USER_AGENTS_DIR`` leaves the rest of the listing
    intact without error.

    Observation-first: these assertions encode the ACTUAL non-SHAI listing
    observed on unfixed code. The invariant is expressed against the non-SHAI
    SUBSET of the listing (entries with ``source != "shai"``), so it holds both
    now — where SHAI contributes nothing, so the subset equals the full listing
    — and after the fix, where SHAI only ever ADDS ``source == "shai"`` entries
    and must never mutate or drop a non-SHAI one.

    Seams follow the existing suite: ``_scan_directory`` is patched to inject a
    generated set of non-SHAI profiles (with controllable source labels and
    name collisions), the built-in store is faked via ``resources.files``, and
    the SHAI side is driven by a real fake ``SHAI_USER_AGENTS_DIR`` (the default
    arg of ``discover_user_agents()``).
    """

    # Deterministic, legal profile-name alphabet (no "/", "\\", or ".." — see
    # _validate_agent_name) so generated SHAI files are real, loadable configs.
    _NAME = st.text(
        alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_",
        min_size=1,
        max_size=12,
    ).filter(lambda s: ".." not in s and not routes_to_ephemeral(s))

    @staticmethod
    def _non_shai_subset(result):
        """The listing's non-SHAI entries, keyed by name (the preserved set)."""
        return {p["name"]: p for p in result if p.get("source") != "shai"}

    @staticmethod
    def _wire_non_shai(
        monkeypatch,
        *,
        local_names,
        provider_names,
        builtin_names,
    ):
        """Patch the non-SHAI discovery seams to a generated profile set."""
        ap = "cli_agent_orchestrator.utils.agent_profiles"
        ss = "cli_agent_orchestrator.services.settings_service"

        monkeypatch.setattr(f"{ss}.get_extra_agent_dirs", lambda: [])
        monkeypatch.setattr(
            f"{ss}.get_agent_dirs",
            lambda: ({"kiro_cli": "/fake/kiro/agents"} if provider_names else {}),
        )

        # Built-in store: real-dir-backed-ish fake so iterdir()/read_text() work.
        builtin_items = []
        for name in builtin_names:
            f = MagicMock()
            f.name = f"{name}.md"
            f.read_text.return_value = f"---\ndescription: builtin {name}\n---\nPrompt"
            builtin_items.append(f)
        mock_agent_store = MagicMock()
        mock_agent_store.iterdir.return_value = builtin_items
        mock_resources = MagicMock()
        mock_resources.files.return_value = mock_agent_store
        monkeypatch.setattr(f"{ap}.resources", mock_resources)

        mock_local_dir = MagicMock()
        mock_local_dir.exists.return_value = True
        mock_local_dir.resolve.return_value = Path("/fake/local-store")
        monkeypatch.setattr(f"{ap}.LOCAL_AGENT_STORE_DIR", mock_local_dir)

        def fake_scan(
            directory, source_label, profiles, name_sources=None, dir_profiles_loadable=True
        ):
            if source_label == "local":
                injected = local_names
            elif source_label == "kiro":
                injected = provider_names
            else:
                injected = []
            for name in injected:
                if name_sources is not None:
                    name_sources.setdefault(name, []).append(source_label)
                if name not in profiles:
                    profiles[name] = {
                        "name": name,
                        "description": f"{source_label} {name}",
                        "source": source_label,
                        "loadable": dir_profiles_loadable,
                    }

        monkeypatch.setattr(f"{ap}._scan_directory", fake_scan)

    @staticmethod
    def _write_native_shai_agents(agents_dir: Path, names, *, legacy: bool = False):
        """Create valid native SHAI configs for each name under ``agents_dir``."""
        import json

        agents_dir.mkdir(parents=True, exist_ok=True)
        suffix = ".config" if legacy else ".config.json"
        for name in names:
            (agents_dir / f"{name}{suffix}").write_text(
                json.dumps(
                    {
                        "name": name,
                        "description": f"Native SHAI agent {name}",
                        "system_prompt": "You are a SHAI agent.",
                        "llm_provider": {"type": "openai"},
                        "tools": {"builtin": [], "builtin_excluded": [], "mcp": {}},
                    }
                ),
                encoding="utf-8",
            )

    @settings(
        max_examples=60,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    @given(
        local_names=st.lists(_NAME, max_size=5, unique=True),
        provider_names=st.lists(_NAME, max_size=5, unique=True),
        builtin_names=st.lists(_NAME, max_size=5, unique=True),
        shai_names=st.lists(_NAME, max_size=5, unique=True),
    )
    def test_non_shai_entries_preserved_across_random_shai_names(
        self,
        local_names,
        provider_names,
        builtin_names,
        shai_names,
        tmp_path,
        monkeypatch,
    ):
        """Random non-SHAI profiles + random native SHAI names (incl. names that
        collide with existing profiles): every non-SHAI entry is preserved
        exactly, the listing stays sorted by name, and dedup/precedence +
        duplicated_in flagging are unchanged.

        Validates: Requirements 3.2

        The invariant compares the non-SHAI subset WITH a populated SHAI dir
        against the non-SHAI subset computed with NO SHAI dir — they must be
        identical. On unfixed code both already equal the full listing, so this
        passes now and remains valid after the fix.
        """
        from cli_agent_orchestrator.utils.agent_profiles import list_agent_profiles

        self._wire_non_shai(
            monkeypatch,
            local_names=local_names,
            provider_names=provider_names,
            builtin_names=builtin_names,
        )

        # Unique per-example subdir: tmp_path is reused across hypothesis
        # examples, so a fresh name keeps each example's SHAI dir independent.
        import uuid

        base = tmp_path / uuid.uuid4().hex

        # Baseline: no SHAI dir present (points the default arg at a missing path).
        monkeypatch.setattr(
            "cli_agent_orchestrator.utils.shai_config.SHAI_USER_AGENTS_DIR",
            base / "absent-shai",
        )
        baseline = list_agent_profiles()
        baseline_non_shai = self._non_shai_subset(baseline)

        # Now populate the SHAI dir with (possibly colliding) native agents.
        shai_dir = base / "shai-agents"
        self._write_native_shai_agents(shai_dir, shai_names)
        monkeypatch.setattr(
            "cli_agent_orchestrator.utils.shai_config.SHAI_USER_AGENTS_DIR", shai_dir
        )
        with_shai = list_agent_profiles()
        with_shai_non_shai = self._non_shai_subset(with_shai)

        # Per the design's precedence rule, SHAI is scanned AFTER the local,
        # provider, and extra sources but BEFORE the built-in store (Change 2:
        # "provider-tier precedence ... shadowed by any earlier-scanned
        # same-named profile"). Two consequences for the non-SHAI subset:
        #   1. A built-in-ONLY name that also names a native SHAI agent is won by
        #      SHAI (built-in is scanned later), so it becomes a "shai" entry and
        #      correctly drops out of the non-SHAI subset.
        #   2. A name won by an EARLIER source (local/provider/extra) keeps that
        #      winner, but — because SHAI participates in conflict flagging
        #      uniformly — the winner's duplicated_in gains "shai" when a native
        #      SHAI agent shares the name. Every other field is unchanged.
        earlier_non_shai_names = set(local_names) | set(provider_names)
        shai_name_set = set(shai_names)
        expected_names = {
            name
            for name in baseline_non_shai
            if name in earlier_non_shai_names or name not in shai_name_set
        }

        # SHAI never ADDS a non-SHAI entry, and only ever removes a built-in name
        # it legitimately wins: the surviving non-SHAI name set is exactly this.
        assert set(with_shai_non_shai) == expected_names

        # Sort order of the whole listing is preserved (alphabetical by name).
        names = [p["name"] for p in with_shai]
        assert names == sorted(names)

        # Every surviving non-SHAI winner keeps its name/source/loadable and
        # discovery fields byte-for-byte; duplicated_in is unchanged except that
        # a colliding native SHAI agent appends "shai" to the shadowed-source
        # list (uniform conflict flagging), never reordering the earlier entries.
        for name in expected_names:
            got = with_shai_non_shai[name]
            base_entry = baseline_non_shai[name]
            for field in ("name", "source", "loadable", "description", "capabilities", "tags"):
                if field in base_entry:
                    assert got.get(field) == base_entry.get(field)
            base_dupe = list(base_entry.get("duplicated_in", []))
            if name in shai_name_set:
                # SHAI is scanned between the earlier sources and the built-in
                # store, so "shai" lands in duplicated_in just before any
                # shadowed "built-in" entry and after any earlier shadowed ones.
                insert_at = base_dupe.index("built-in") if "built-in" in base_dupe else len(base_dupe)
                expected_dupe = base_dupe[:insert_at] + ["shai"] + base_dupe[insert_at:]
            else:
                expected_dupe = base_dupe
            assert got.get("duplicated_in") == expected_dupe

    @settings(
        max_examples=50,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    @given(
        local_names=st.lists(_NAME, max_size=4, unique=True),
        builtin_names=st.lists(_NAME, max_size=4, unique=True),
        dir_state=st.sampled_from(["absent", "empty", "files", "malformed"]),
    )
    def test_non_shai_listing_invariant_across_shai_dir_states(
        self,
        local_names,
        builtin_names,
        dir_state,
        tmp_path,
        monkeypatch,
    ):
        """Random SHAI_USER_AGENTS_DIR states (absent, empty, files present, one
        unreadable/malformed): the non-SHAI listing is invariant and no
        exception escapes list_agent_profiles().

        Validates: Requirements 3.3
        """
        from cli_agent_orchestrator.utils.agent_profiles import list_agent_profiles

        self._wire_non_shai(
            monkeypatch,
            local_names=local_names,
            provider_names=[],
            builtin_names=builtin_names,
        )

        # Unique per-example subdir: hypothesis reuses tmp_path across examples,
        # so a fresh name keeps each SHAI dir state independent (an "absent"
        # example must not see a dir an earlier "files" example created).
        import uuid

        base = tmp_path / uuid.uuid4().hex

        # Reference non-SHAI listing with a definitely-absent SHAI dir.
        monkeypatch.setattr(
            "cli_agent_orchestrator.utils.shai_config.SHAI_USER_AGENTS_DIR",
            base / "reference-absent",
        )
        reference = self._non_shai_subset(list_agent_profiles())

        # Build the generated SHAI dir state.
        shai_dir = base / "shai-agents"
        if dir_state == "absent":
            pass  # never created
        elif dir_state == "empty":
            shai_dir.mkdir(parents=True, exist_ok=True)
        elif dir_state == "files":
            self._write_native_shai_agents(shai_dir, ["alpha", "beta"])
        elif dir_state == "malformed":
            shai_dir.mkdir(parents=True, exist_ok=True)
            # One valid, one malformed (non-JSON) native file.
            self._write_native_shai_agents(shai_dir, ["good"])
            (shai_dir / "broken.config.json").write_text("{ not valid json", encoding="utf-8")
        monkeypatch.setattr(
            "cli_agent_orchestrator.utils.shai_config.SHAI_USER_AGENTS_DIR", shai_dir
        )

        # Must not raise regardless of SHAI dir state.
        result = list_agent_profiles()

        # The non-SHAI subset is invariant across every SHAI dir state.
        assert self._non_shai_subset(result) == reference

    @patch("cli_agent_orchestrator.services.settings_service.get_extra_agent_dirs", return_value=[])
    @patch("cli_agent_orchestrator.services.settings_service.get_agent_dirs", return_value={})
    @patch("cli_agent_orchestrator.utils.agent_profiles._scan_directory")
    @patch("cli_agent_orchestrator.utils.agent_profiles.LOCAL_AGENT_STORE_DIR")
    @patch("cli_agent_orchestrator.utils.agent_profiles.resources")
    def test_discovery_does_not_modify_shai_user_dir(
        self,
        mock_resources,
        mock_local_dir,
        mock_scan,
        mock_get_agent_dirs,
        mock_get_extra_dirs,
        tmp_path,
        monkeypatch,
    ):
        """Read-only contract: after discovery runs against a populated
        SHAI_USER_AGENTS_DIR, nothing under SHAI_USER_* is created or modified
        (directory contents and mtimes unchanged).

        Validates: Requirements 3.1, 3.4
        """
        from cli_agent_orchestrator.utils.agent_profiles import list_agent_profiles

        mock_agent_store = MagicMock()
        mock_agent_store.iterdir.return_value = []
        mock_resources.files.return_value = mock_agent_store
        mock_local_dir.exists.return_value = False

        shai_dir = tmp_path / "shai-agents"
        self._write_native_shai_agents(shai_dir, ["AGENTS_CONTROL_AGENT", "worker"])
        self._write_native_shai_agents(shai_dir, ["legacy"], legacy=True)
        monkeypatch.setattr(
            "cli_agent_orchestrator.utils.shai_config.SHAI_USER_AGENTS_DIR", shai_dir
        )

        def snapshot(root: Path):
            # name -> (size, mtime_ns) for the dir and every entry under it.
            snap = {}
            for p in sorted(root.rglob("*")):
                st_ = p.stat()
                snap[str(p.relative_to(root))] = (p.is_file(), st_.st_size, st_.st_mtime_ns)
            dir_st = root.stat()
            snap["."] = (False, None, dir_st.st_mtime_ns)
            return snap

        before = snapshot(shai_dir)

        list_agent_profiles()

        after = snapshot(shai_dir)
        assert after == before, (
            "read-only contract violated: discovery created or modified "
            f"something under SHAI_USER_AGENTS_DIR. before={before} after={after}"
        )


class TestScanDirectory:
    """Tests for _scan_directory helper function."""

    def test_scan_directory_skips_nonexistent_directory(self, tmp_path):
        """Test that _scan_directory does nothing for nonexistent directories."""
        from cli_agent_orchestrator.utils.agent_profiles import _scan_directory

        nonexistent = tmp_path / "does_not_exist"
        profiles = {}
        _scan_directory(nonexistent, "test", profiles)
        assert profiles == {}

    def test_scan_directory_finds_md_files(self, tmp_path):
        """Test that _scan_directory finds .md files in a directory."""
        from cli_agent_orchestrator.utils.agent_profiles import _scan_directory

        # Create a .md file with frontmatter
        md_file = tmp_path / "my-agent.md"
        md_file.write_text("---\ndescription: My agent\n---\nSystem prompt")

        profiles = {}
        _scan_directory(tmp_path, "local", profiles)

        assert "my-agent" in profiles
        assert profiles["my-agent"]["name"] == "my-agent"
        assert profiles["my-agent"]["description"] == "My agent"
        assert profiles["my-agent"]["source"] == "local"

    def test_scan_directory_finds_subdirectory_profiles(self, tmp_path):
        """Test that _scan_directory finds agent.md inside subdirectories."""
        from cli_agent_orchestrator.utils.agent_profiles import _scan_directory

        # Create a subdirectory with agent.md
        agent_dir = tmp_path / "sub-agent"
        agent_dir.mkdir()
        agent_md = agent_dir / "agent.md"
        agent_md.write_text("---\ndescription: Sub agent\n---\nPrompt content")

        profiles = {}
        _scan_directory(tmp_path, "kiro", profiles)

        assert "sub-agent" in profiles
        assert profiles["sub-agent"]["description"] == "Sub agent"
        assert profiles["sub-agent"]["source"] == "kiro"

    def test_scan_directory_does_not_overwrite_existing_profile(self, tmp_path):
        """Test that _scan_directory does not overwrite an already-discovered profile."""
        from cli_agent_orchestrator.utils.agent_profiles import _scan_directory

        md_file = tmp_path / "existing.md"
        md_file.write_text("---\ndescription: New version\n---\nPrompt")

        profiles = {
            "existing": {
                "name": "existing",
                "description": "Original version",
                "source": "built-in",
            }
        }
        _scan_directory(tmp_path, "local", profiles)

        # Should retain the original profile
        assert profiles["existing"]["description"] == "Original version"
        assert profiles["existing"]["source"] == "built-in"


class TestLoadAgentProfileEnvResolution:
    """Tests for env var resolution during agent profile loading."""

    @staticmethod
    def _write_profile(profile_path: Path, body: str) -> None:
        profile_path.write_text(
            "---\n"
            "name: service-agent\n"
            "description: Service agent\n"
            "mcpServers:\n"
            "  service:\n"
            "    command: service-mcp\n"
            "    env:\n"
            "      API_TOKEN: ${API_TOKEN}\n"
            "      OPTIONAL_VALUE: ${OPTIONAL_VALUE}\n"
            "---\n"
            f"{body}\n"
        )

    @patch("cli_agent_orchestrator.services.settings_service.get_extra_agent_dirs", return_value=[])
    @patch("cli_agent_orchestrator.services.settings_service.get_agent_dirs", return_value={})
    def test_load_agent_profile_resolves_vars_in_mcp_servers_and_body(
        self, mock_get_agent_dirs, mock_get_extra_dirs, tmp_path, monkeypatch
    ):
        """Vars in frontmatter and body should resolve from the managed env file."""
        local_store_dir = tmp_path / "agent-store"
        local_store_dir.mkdir()
        env_file = tmp_path / ".env"
        env_file.write_text("API_TOKEN=resolved-secret\nOPTIONAL_VALUE=resolved-optional\n")
        profile_path = local_store_dir / "service-agent.md"
        self._write_profile(profile_path, "Body token: ${API_TOKEN}")

        monkeypatch.setattr(
            "cli_agent_orchestrator.utils.agent_profiles.LOCAL_AGENT_STORE_DIR", local_store_dir
        )
        monkeypatch.setattr("cli_agent_orchestrator.utils.env.CAO_ENV_FILE", env_file)

        profile = load_agent_profile("service-agent")

        assert profile.system_prompt == "Body token: resolved-secret"
        assert profile.mcpServers is not None
        assert profile.mcpServers["service"]["env"] == {
            "API_TOKEN": "resolved-secret",
            "OPTIONAL_VALUE": "resolved-optional",
        }

    @patch("cli_agent_orchestrator.services.settings_service.get_extra_agent_dirs", return_value=[])
    @patch("cli_agent_orchestrator.services.settings_service.get_agent_dirs", return_value={})
    def test_load_agent_profile_leaves_missing_vars_intact(
        self, mock_get_agent_dirs, mock_get_extra_dirs, tmp_path, monkeypatch
    ):
        """Missing vars should remain as placeholders without raising."""
        local_store_dir = tmp_path / "agent-store"
        local_store_dir.mkdir()
        profile_path = local_store_dir / "service-agent.md"
        self._write_profile(profile_path, "Body token: ${API_TOKEN}")

        monkeypatch.setattr(
            "cli_agent_orchestrator.utils.agent_profiles.LOCAL_AGENT_STORE_DIR", local_store_dir
        )
        monkeypatch.setattr("cli_agent_orchestrator.utils.env.CAO_ENV_FILE", tmp_path / ".env")

        profile = load_agent_profile("service-agent")

        assert profile.system_prompt == "Body token: ${API_TOKEN}"
        assert profile.mcpServers is not None
        assert profile.mcpServers["service"]["env"]["API_TOKEN"] == "${API_TOKEN}"

    @patch("cli_agent_orchestrator.services.settings_service.get_extra_agent_dirs", return_value=[])
    @patch("cli_agent_orchestrator.services.settings_service.get_agent_dirs", return_value={})
    def test_load_agent_profile_without_placeholders_matches_existing_behavior(
        self, mock_get_agent_dirs, mock_get_extra_dirs, tmp_path, monkeypatch
    ):
        """Profiles without placeholders should load unchanged."""
        local_store_dir = tmp_path / "agent-store"
        local_store_dir.mkdir()
        profile_path = local_store_dir / "plain-agent.md"
        profile_path.write_text(
            "---\nname: plain-agent\ndescription: Plain agent\n---\nPlain system prompt\n"
        )

        monkeypatch.setattr(
            "cli_agent_orchestrator.utils.agent_profiles.LOCAL_AGENT_STORE_DIR", local_store_dir
        )
        monkeypatch.setattr("cli_agent_orchestrator.utils.env.CAO_ENV_FILE", tmp_path / ".env")

        profile = load_agent_profile("plain-agent")

        assert profile.name == "plain-agent"
        assert profile.description == "Plain agent"
        assert profile.system_prompt == "Plain system prompt"

    @patch("cli_agent_orchestrator.services.settings_service.get_extra_agent_dirs", return_value=[])
    @patch("cli_agent_orchestrator.services.settings_service.get_agent_dirs", return_value={})
    @patch("cli_agent_orchestrator.utils.agent_profiles.resources.files")
    def test_load_agent_profile_builtin_store_fallback_resolves_vars(
        self, mock_files, mock_get_agent_dirs, mock_get_extra_dirs, tmp_path, monkeypatch
    ):
        """Built-in store fallback should apply env resolution before parsing."""
        builtin_store_dir = tmp_path / "builtin-store"
        builtin_store_dir.mkdir()
        env_file = tmp_path / ".env"
        env_file.write_text("API_TOKEN=builtin-secret\n")
        profile_path = builtin_store_dir / "service-agent.md"
        self._write_profile(profile_path, "Body token: ${API_TOKEN}")

        mock_files.return_value = builtin_store_dir
        monkeypatch.setattr(
            "cli_agent_orchestrator.utils.agent_profiles.LOCAL_AGENT_STORE_DIR",
            tmp_path / "missing-local-store",
        )
        monkeypatch.setattr("cli_agent_orchestrator.utils.env.CAO_ENV_FILE", env_file)

        profile = load_agent_profile("service-agent")

        assert profile.system_prompt == "Body token: builtin-secret"
        assert profile.mcpServers is not None
        assert profile.mcpServers["service"]["env"]["API_TOKEN"] == "builtin-secret"


class TestCodexConfigParsing:
    """codexConfig frontmatter parses into the AgentProfile field."""

    def test_codex_config_parses_dotted_keys_and_mixed_value_types(self):
        text = (
            "---\n"
            "name: codex-agent\n"
            "description: Codex agent with inline config\n"
            "provider: codex\n"
            "codexConfig:\n"
            '  model_reasoning_effort: "xhigh"\n'
            '  service_tier: "fast"\n'
            "  features.fast_mode: true\n"
            "---\n"
            "System prompt content"
        )

        profile = parse_agent_profile_text(text, "codex-agent")

        assert profile.codexConfig == {
            "model_reasoning_effort": "xhigh",
            "service_tier": "fast",
            "features.fast_mode": True,
        }

    def test_codex_config_defaults_to_none_when_absent(self):
        text = (
            "---\n"
            "name: codex-agent\n"
            "description: Codex agent without inline config\n"
            "provider: codex\n"
            "---\n"
            "System prompt content"
        )

        profile = parse_agent_profile_text(text, "codex-agent")

        assert profile.codexConfig is None


class TestGrokNativeWorkflowsParsing:
    """Grok's native-workflow opt-in is a typed profile setting."""

    def test_grok_native_workflows_is_opt_in(self):
        text = (
            "---\n"
            "name: grok-native\n"
            "description: Grok agent with native workers\n"
            "provider: grok_cli\n"
            "grokNativeWorkflows: true\n"
            "---\n"
            "System prompt content"
        )

        profile = parse_agent_profile_text(text, "grok-native")

        assert profile.grokNativeWorkflows is True

    def test_omitted_grok_native_workflows_preserves_profile_api_compatibility(self):
        profile = AgentProfile(name="grok-default", description="Grok agent")

        assert profile.grokNativeWorkflows is None
