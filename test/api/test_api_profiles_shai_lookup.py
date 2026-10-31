"""Bugfix spec `shai-profile-lookup-by-id` — Task 4 integration tests.

HTTP-layer coverage for the native-SHAI-lookup fix, driving the assembled
FastAPI app in-process through the shared ``client`` fixture
(``test/api/conftest.py``). These exercise the real route handlers
(``GET /agents/profiles`` → ``list_agent_profiles`` and
``GET /agents/profiles/{name}`` → ``load_agent_profile`` in
``cli_agent_orchestrator.api.main``) against native SHAI configs on disk — no
mocking of the lookup itself, only store isolation via monkeypatch:

- ``GET /agents/profiles/{name}`` for a native-SHAI-only id returns HTTP 200
  with the parsed profile body, not the pre-fix 404 (Req 2.1, 2.2);
- full flow — enumerate ``GET /agents/profiles``, then fetch each listed
  ``shai`` id by-id and assert all resolve with HTTP 200, confirming
  list/lookup symmetry (Req 2.1, 2.2, 3.3).

Validates: Requirements 2.1, 2.2, 2.3, 3.3
"""

import json
from pathlib import Path

import pytest

# Stores live in the utils module; the route handlers call through to it.
AP = "cli_agent_orchestrator.utils.agent_profiles"
SS = "cli_agent_orchestrator.services.settings_service"
SHAI = "cli_agent_orchestrator.utils.shai_config.SHAI_USER_AGENTS_DIR"


def _write_native_shai_config(
    agents_dir: Path,
    name: str,
    *,
    description: str = "",
    llm_provider: dict | None = None,
    tools: dict | None = None,
) -> Path:
    """Write one native SHAI agent config (`<name>.config.json`).

    The config ``name`` field is uppercased on purpose so a passing test proves
    the HTTP lookup keys off the FILENAME-derived id the list surfaced.
    """
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


@pytest.fixture
def isolated_native_shai_only(tmp_path, monkeypatch):
    """Isolate every store so the only possible source is the native SHAI dir.

    Returns the native SHAI directory so a test can populate it. The local,
    provider, extra, and built-in stores are pointed at empty/missing
    locations, matching the ``isBugCondition`` precondition (name resolvable
    ONLY under ``SHAI_USER_AGENTS_DIR``).
    """
    monkeypatch.setattr(f"{AP}.resolve_env_vars", lambda text: text)

    empty_local = tmp_path / "empty-local"
    empty_local.mkdir()
    monkeypatch.setattr(f"{AP}.LOCAL_AGENT_STORE_DIR", empty_local)

    monkeypatch.setattr(f"{SS}.get_agent_dirs", lambda: {})
    monkeypatch.setattr(f"{SS}.get_extra_agent_dirs", lambda: [])
    monkeypatch.setattr(f"{SS}.get_disabled_agent_dirs", lambda: [])

    empty_builtin = tmp_path / "empty-builtin"
    empty_builtin.mkdir()
    monkeypatch.setattr(f"{AP}.resources.files", lambda _pkg: empty_builtin)

    shai_dir = tmp_path / "shai-agents"
    shai_dir.mkdir()
    monkeypatch.setattr(SHAI, shai_dir)
    return shai_dir


class TestGetNativeShaiProfileByIdOverHttp:
    """GET /agents/profiles/{name} for a native-SHAI-only id returns 200."""

    def test_native_shai_only_id_returns_200_with_parsed_body(
        self, client, isolated_native_shai_only
    ):
        """The canonical repro: ``agents_control_agent`` is native-SHAI-only.

        Pre-fix this returned 404 ("Agent profile not found"); post-fix the
        by-id endpoint resolves it and returns 200 with the parsed profile —
        provider/model/tools mapped from the SHAI config.
        """
        _write_native_shai_config(
            isolated_native_shai_only,
            "agents_control_agent",
            description="Controls agents",
            llm_provider={"provider": "openai", "model": "gpt-4o"},
            tools={"builtin": ["fs_read"], "builtin_excluded": [], "mcp": {}},
        )

        response = client.get("/agents/profiles/agents_control_agent")

        assert response.status_code == 200, response.text
        body = response.json()
        # Filename-derived id, NOT the uppercased config "name" field.
        assert body["name"] == "agents_control_agent"
        assert body["description"] == "Controls agents"
        assert body["provider"] == "openai"
        assert body["model"] == "gpt-4o"
        assert body["tools"] == ["fs_read"]

    def test_genuinely_missing_id_still_returns_404(self, client, isolated_native_shai_only):
        """An id absent from every store, including the native SHAI dir, still
        returns HTTP 404 (genuinely-missing ids stay not-found)."""
        _write_native_shai_config(isolated_native_shai_only, "present_agent")

        response = client.get("/agents/profiles/does_not_exist")

        assert response.status_code == 404, response.text
        assert "Agent profile not found" in response.json()["detail"]


class TestListLookupSymmetryOverHttp:
    """Full flow: every id the list surfaces with source 'shai' is resolvable
    by-id with HTTP 200 (list/lookup symmetry)."""

    def test_every_listed_shai_id_resolves_by_id_with_200(self, client, isolated_native_shai_only):
        """Enumerate GET /agents/profiles, then fetch each listed ``shai`` id
        by-id and assert all resolve with HTTP 200 (Req 2.1, 2.2, 3.3)."""
        names = ["agents_control_agent", "reviewer", "helper"]
        for name in names:
            _write_native_shai_config(
                isolated_native_shai_only,
                name,
                description=f"{name} description",
                llm_provider={"provider": "anthropic", "model": "claude-3-5"},
            )

        list_response = client.get("/agents/profiles")
        assert list_response.status_code == 200, list_response.text
        listed = list_response.json()

        shai_ids = [p["name"] for p in listed if p.get("source") == "shai"]
        # Precondition: the list surfaces exactly the native SHAI agents we wrote.
        assert sorted(shai_ids) == sorted(
            names
        ), f"list did not surface the expected shai ids: {sorted(shai_ids)}"

        # Every id the list promises must resolve by-id with HTTP 200.
        for name in shai_ids:
            by_id = client.get(f"/agents/profiles/{name}")
            assert by_id.status_code == 200, (
                f"GET /agents/profiles/{name} returned " f"{by_id.status_code}: {by_id.text}"
            )
            assert by_id.json()["name"] == name
