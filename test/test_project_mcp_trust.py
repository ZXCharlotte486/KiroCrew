"""Opt-in consent for a project checkout's own MCP servers on mirrored sessions.

``kiro_crew.project_mcp_trust`` is the consent; ``session_mcp._project_mcp_trusted``
reads it. Without a grant, a checkout's spec reaches the ``session/new`` array only
as switch-off keys (pinned in ``test_session_mcp_project_trust.py``). With one, the
project spec is returned whole, so its servers mount on every array-backed host and
its ``disabledTools`` and mutes still restrict. Every unreadable, garbled or
ambiguous input fails closed, and a project-skills grant never stands in for it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew import agent as agent_mod
from kiro_crew import project_mcp_trust, skill_trust
from kiro_crew.acp import session_mcp
from kiro_crew.acp_backends import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_CODEX,
    ACP_BACKEND_GOOSE,
    ACP_BACKEND_KIRO,
    ACP_BACKEND_OPENCODE,
)
from kiro_crew.config.loader import KiroCrewConfig, SkillsConfig
from kiro_crew.dashboard.handlers import mcp_project_trust as handlers
from kiro_crew.providers.mirrors.registry import mirror_for

pytestmark = pytest.mark.skipif(
    not skill_trust.project_skill_traversal_supported(),
    reason="project trust requires no-follow directory-descriptor traversal",
)

_CORE = {"command": "/opt/kirocrew", "args": ["mcp-core"]}
_REPO_CMD = "/nonexistent/project-mcp-trust/repo-launcher"
_MIRRORED = [ACP_BACKEND_CLAUDE, ACP_BACKEND_CODEX, ACP_BACKEND_GOOSE, ACP_BACKEND_OPENCODE]


@pytest.fixture(autouse=True)
def _isolated(tmp_path, _floor_monkeypatch):
    """Data home, agents dir and settings in tmp; both trust caches dropped."""
    mp = _floor_monkeypatch
    mp.setenv("KIROCREW_HOME", str(tmp_path / "crew-home"))
    agents = tmp_path / "home-agents"
    agents.mkdir()
    mp.setattr(agent_mod, "KIRO_AGENTS_DIR", agents)
    mp.setattr(agent_mod, "_KIRO_MCP_JSON", tmp_path / "settings-mcp.json")
    mp.setattr(session_mcp, "ensure_agent_materialized", lambda _a: True)
    mp.setattr(
        session_mcp,
        "managed_mcp_spec_entry",
        lambda name: {"kirocrew-core": dict(_CORE)}.get(name),
    )
    mp.setattr(session_mcp, "_mcp_registry_mode", lambda: False)
    project_mcp_trust.reset_cache_for_tests()
    skill_trust.reset_cache_for_tests()
    yield
    project_mcp_trust.reset_cache_for_tests()
    skill_trust.reset_cache_for_tests()


def _checkout(root: Path, *, servers: dict | None = None, tools: list | None = None) -> Path:
    checkout = root / "repo"
    agents = checkout / ".kiro" / "agents"
    agents.mkdir(parents=True)
    (agents / "helper.json").write_text(
        json.dumps(
            {
                "name": "kirocrew",
                "mcpServers": servers
                or {
                    "repo-srv": {"command": _REPO_CMD, "args": []},
                    "kirocrew-core": {"disabledTools": ["spawn_run"]},
                },
                "tools": tools or ["@repo-srv", "@kirocrew-core"],
            }
        ),
        encoding="utf-8",
    )
    return checkout


def _array(backend: str, work_dir: Path) -> list[dict]:
    mirror = mirror_for(backend)
    assert mirror is not None
    params = mirror.session_params(
        "kirocrew", work_dir=str(work_dir), permission_surface_owned=True
    )
    servers = params.get("mcpServers")
    assert isinstance(servers, list)
    return servers


def _commands(servers: list[dict]) -> set[str]:
    return {str(s.get("command")) for s in servers if s.get("command")}


def _names(servers: list[dict]) -> set[str]:
    return {str(s.get("name")) for s in servers}


def _trusted(project: Path) -> bool:
    """The dashboard's verdict: every current agent spec matches the grant."""
    return project_mcp_trust.is_launch_set_trusted(
        project, project_mcp_trust.current_launch_fingerprints(project)
    )


def _launch(project: Path) -> dict[str, str]:
    return project_mcp_trust.current_launch_fingerprints(project)


def _write_store(payload: object) -> None:
    path = project_mcp_trust.store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    text = payload if isinstance(payload, str) else json.dumps(payload)
    path.write_text(text, encoding="utf-8")
    project_mcp_trust.reset_cache_for_tests()


# ── session array, all four mirrored backends ──


@pytest.mark.parametrize("backend", _MIRRORED)
def test_untrusted_projects_server_is_absent(backend, tmp_path):
    assert _REPO_CMD not in _commands(_array(backend, _checkout(tmp_path)))


@pytest.mark.parametrize("backend", _MIRRORED)
def test_granted_projects_server_is_present(backend, tmp_path):
    checkout = _checkout(tmp_path)
    project_mcp_trust.grant_project_mcp_trust(checkout)
    assert _REPO_CMD in _commands(_array(backend, checkout))


@pytest.mark.parametrize("backend", _MIRRORED)
def test_revoke_makes_the_server_absent_again(backend, tmp_path):
    checkout = _checkout(tmp_path)
    project_mcp_trust.grant_project_mcp_trust(checkout)
    assert project_mcp_trust.revoke_project_mcp_trust(checkout) is True
    assert _REPO_CMD not in _commands(_array(backend, checkout))


@pytest.mark.parametrize("backend", _MIRRORED)
def test_a_skills_grant_is_not_mcp_trust(backend, tmp_path, _floor_monkeypatch):
    checkout = _checkout(tmp_path)
    _floor_monkeypatch.setattr(
        KiroCrewConfig,
        "load",
        classmethod(
            lambda cls, *a, **k: KiroCrewConfig(skills=SkillsConfig(project_skills_enabled=True))
        ),
    )
    skill_trust.grant_project_trust(checkout)
    assert skill_trust.is_project_trusted(checkout) is True, "precondition: skills grant landed"
    assert _REPO_CMD not in _commands(_array(backend, checkout))


@pytest.mark.parametrize("backend", _MIRRORED)
def test_trusted_spec_restrictions_still_reach_each_backends_deny_channel(backend, tmp_path):
    """``return spec, None`` keeps ``disabledTools`` and mutes on every backend."""
    checkout = _checkout(
        tmp_path,
        servers={
            "repo-srv": {"command": _REPO_CMD, "args": []},
            "kirocrew-core": {"disabledTools": ["spawn_run"]},
            "muted-srv": {"command": "/nonexistent/project-mcp-trust/muted", "disabled": True},
        },
        tools=["@repo-srv", "@kirocrew-core", "@muted-srv"],
    )
    project_mcp_trust.grant_project_mcp_trust(checkout)
    projection = session_mcp.session_mcp_projection("kirocrew", work_dir=checkout)
    assert ("kirocrew-core", "spawn_run") in projection.disabled_tools
    assert "muted-srv" in projection.disabled_servers
    mirror = mirror_for(backend)
    assert mirror is not None
    face = mirror.session_projection(
        "kirocrew", work_dir=str(checkout), permission_surface_owned=True
    )
    names = _names(face.params["mcpServers"])
    assert "muted-srv" not in names
    assert "repo-srv" in names
    if backend == ACP_BACKEND_CLAUDE:
        rules = session_mcp.session_mcp_deny_rules("kirocrew", work_dir=checkout)
        assert "mcp__kirocrew-core__spawn_run" in rules
    elif backend == ACP_BACKEND_CODEX:
        assert ("kirocrew-core", "spawn_run") in face.denied_tools
    elif backend == ACP_BACKEND_OPENCODE:
        # The server stays mounted and the harness is told to deny the one tool.
        assert "kirocrew-core_spawn_run" in face.harness_deny_rules
    else:
        # goose has no safe per-tool deny channel: the server is withheld.
        assert "kirocrew-core" not in names


def test_kiro_cli_path_is_unchanged_by_a_grant(tmp_path):
    checkout = _checkout(tmp_path)
    assert mirror_for(ACP_BACKEND_KIRO) is None
    before = session_mcp._agent_spec_and_snapshot_for("kirocrew", checkout)[0]
    project_mcp_trust.grant_project_mcp_trust(checkout)
    after = session_mcp._agent_spec_and_snapshot_for("kirocrew", checkout)[0]
    assert before == after
    assert before is not None and "repo-srv" in before["mcpServers"]


# ── the gate fails closed ──


def test_a_skills_store_copied_over_the_mcp_store_grants_nothing(tmp_path):
    checkout = _checkout(tmp_path)
    key = os.path.realpath(checkout)
    identity = skill_trust._instance_identity(key)
    _write_store(
        {
            "version": 1,
            "granted": [{"path": key, "identity": identity, "launch": _launch(checkout)}],
        }
    )
    assert _trusted(checkout) is False


@pytest.mark.parametrize(
    "payload",
    [
        "{not json",
        "[]",
        {"version": 2, "kind": "project-mcp", "granted": []},
        {"version": 1, "kind": "project-skills", "granted": []},
        {"version": 1, "kind": "project-mcp", "granted": "x"},
    ],
    ids=["bad-json", "not-object", "wrong-version", "wrong-kind", "granted-not-list"],
)
def test_a_garbled_store_grants_nothing(payload, tmp_path):
    checkout = _checkout(tmp_path)
    project_mcp_trust.grant_project_mcp_trust(checkout)
    if isinstance(payload, dict) and payload.get("granted") == []:
        key = os.path.realpath(checkout)
        payload = {
            **payload,
            "granted": [
                {
                    "path": key,
                    "identity": skill_trust._instance_identity(key),
                    "launch": _launch(checkout),
                }
            ],
        }
    _write_store(payload)
    assert _trusted(checkout) is False
    assert _REPO_CMD not in _commands(_array(ACP_BACKEND_CLAUDE, checkout))


def test_an_unbound_row_grants_nothing_but_is_listed(tmp_path):
    checkout = _checkout(tmp_path)
    key = os.path.realpath(checkout)
    _write_store({"version": 1, "kind": "project-mcp", "granted": [{"path": key}]})
    assert _trusted(checkout) is False
    rows = project_mcp_trust.list_mcp_trusted_projects()
    assert [(r["path"], r["bound"]) for r in rows] == [(key, False)]


def test_no_store_grants_nothing(tmp_path):
    assert not project_mcp_trust.store_path().exists()
    assert _trusted(_checkout(tmp_path)) is False


def test_an_unreadable_store_grants_nothing(tmp_path, _floor_monkeypatch):
    checkout = _checkout(tmp_path)
    project_mcp_trust.grant_project_mcp_trust(checkout)
    project_mcp_trust.reset_cache_for_tests()

    def _refuse(self, *a, **k):
        raise PermissionError("denied")

    _floor_monkeypatch.setattr(Path, "read_text", _refuse)
    assert _trusted(checkout) is False


def test_a_symlinked_project_dir_is_not_trusted(tmp_path):
    checkout = _checkout(tmp_path)
    project_mcp_trust.grant_project_mcp_trust(checkout)
    link = tmp_path / "alias"
    try:
        link.symlink_to(checkout, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable on this platform")
    assert _trusted(checkout) is True, "precondition"
    assert _trusted(link) is False
    with pytest.raises(ValueError):
        project_mcp_trust.grant_project_mcp_trust(link)


def test_a_directory_recreated_at_the_same_path_is_not_trusted(tmp_path):
    checkout = _checkout(tmp_path)
    project_mcp_trust.grant_project_mcp_trust(checkout)
    moved = tmp_path / "moved"
    checkout.rename(moved)
    checkout.mkdir()
    assert _trusted(checkout) is False


def test_an_unsupported_platform_is_not_trusted(tmp_path, _floor_monkeypatch):
    checkout = _checkout(tmp_path)
    project_mcp_trust.grant_project_mcp_trust(checkout)
    _floor_monkeypatch.setattr(skill_trust, "_PROJECT_SKILL_TRAVERSAL_SUPPORTED", False)
    assert _trusted(checkout) is False


def test_expected_key_mismatch_is_refused(tmp_path):
    checkout = _checkout(tmp_path)
    with pytest.raises(project_mcp_trust.ReviewedProjectChanged):
        project_mcp_trust.grant_project_mcp_trust(checkout, expected_key="/somewhere/else")
    assert not project_mcp_trust.store_path().exists()


def test_a_grant_refuses_to_overwrite_an_unreadable_store(tmp_path):
    _write_store("{not json")
    with pytest.raises(project_mcp_trust.TrustStoreUnreadable):
        project_mcp_trust.grant_project_mcp_trust(_checkout(tmp_path))
    assert project_mcp_trust.store_path().read_text(encoding="utf-8") == "{not json"


def test_store_shape_and_mode(tmp_path):
    checkout = _checkout(tmp_path)
    key = project_mcp_trust.grant_project_mcp_trust(checkout)
    data = json.loads(project_mcp_trust.store_path().read_text(encoding="utf-8"))
    assert data["version"] == 1 and data["kind"] == "project-mcp"
    assert [row["path"] for row in data["granted"]] == [key]
    assert data["granted"][0]["identity"]
    assert data["granted"][0]["launch"] == _launch(checkout)
    if os.name == "posix":
        assert (project_mcp_trust.store_path().stat().st_mode & 0o777) == 0o600
    assert not skill_trust.store_path().exists()


# ── routes ──


async def _client(project: Path | None) -> TestClient:
    from dashboard_owner_helpers import as_owner

    app = web.Application()
    app.router.add_get("/api/mcp/project-trust", handlers.api_mcp_project_trust)
    app.router.add_post("/api/mcp/project-trust", handlers.api_mcp_project_trust_grant)
    app.router.add_delete("/api/mcp/project-trust", handlers.api_mcp_project_trust_revoke)
    as_owner(app)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


@pytest.fixture
def slot_project(_floor_monkeypatch):
    box: dict[str, Path | None] = {"dir": None}
    _floor_monkeypatch.setattr(handlers, "requesting_slot_project", lambda *_a: box["dir"])
    return box


@pytest.mark.asyncio
async def test_non_owner_is_refused_by_every_route(tmp_path, slot_project):
    slot_project["dir"] = _checkout(tmp_path)
    client = await _client(slot_project["dir"])
    try:
        for method in ("get", "post", "delete"):
            resp = await getattr(client, method)(
                "/api/mcp/project-trust", headers={"X-Test-App": "some-app"}
            )
            assert resp.status == 403, method
            assert (await resp.json())["code"] == "owner_only", method
    finally:
        await client.close()
    assert not project_mcp_trust.store_path().exists()


@pytest.mark.asyncio
async def test_owner_grant_and_revoke_round_trip(tmp_path, slot_project):
    checkout = _checkout(tmp_path)
    slot_project["dir"] = checkout
    client = await _client(checkout)
    try:
        state = await (await client.get("/api/mcp/project-trust")).json()
        assert state["trusted"] is False
        assert [row["name"] for row in state["servers"]] == ["repo-srv"]
        resp = await client.post(
            "/api/mcp/project-trust",
            json={"expected_key": state["project_key"], "expected_launch": state["launch"]},
        )
        assert resp.status == 200
        granted = await resp.json()
        assert granted["trusted"] is True
        assert [g["path"] for g in granted["grants"]] == [state["project_key"]]
        resp = await client.delete("/api/mcp/project-trust", params={"path": state["project_key"]})
        body = await resp.json()
        assert body["removed"] is True and body["trusted"] is False and body["grants"] == []
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_grant_ignores_a_body_path_and_needs_the_reviewed_key(tmp_path, slot_project):
    checkout = _checkout(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    slot_project["dir"] = checkout
    client = await _client(checkout)
    try:
        resp = await client.post(
            "/api/mcp/project-trust", json={"path": str(other), "expected_key": str(other)}
        )
        assert resp.status == 409
        assert (await resp.json())["code"] == "mcp_trust_project_changed"
        resp = await client.post("/api/mcp/project-trust", json={})
        assert resp.status == 409
    finally:
        await client.close()
    assert project_mcp_trust.list_mcp_trusted_projects() == []


@pytest.mark.asyncio
async def test_grant_with_no_project_is_a_coded_400(slot_project):
    client = await _client(None)
    try:
        resp = await client.post("/api/mcp/project-trust", json={"expected_key": "/x"})
        assert resp.status == 400
        assert (await resp.json())["code"] == "mcp_trust_no_project"
    finally:
        await client.close()


# ── the store is sealed against sandboxed writers ──


def test_store_is_its_own_sealed_leaf_not_under_trust():
    path = project_mcp_trust.store_path()
    assert path.parent.name == "mcp-project-trust"
    assert path.name == "grants.json"
    assert "trust" not in {p.name for p in path.parents if p.name != "mcp-project-trust"}


def test_agent_tools_cannot_write_the_grant_file():
    from kiro_crew.security.paths import is_sensitive_path, is_sensitive_write_path

    for home in (".kiro/crew", ".kirocrew"):
        path = str(Path.home() / home / "mcp-project-trust" / "grants.json")
        assert is_sensitive_write_path(path), path
        # Write-protected, not hidden: reading a grant decides nothing in-sandbox.
        assert not is_sensitive_path(path), path


def test_grant_dir_is_read_only_in_the_sandbox(tmp_path, _floor_monkeypatch):
    from kiro_crew import sandbox

    _floor_monkeypatch.setattr(sandbox, "config_dir", lambda: tmp_path)
    dirs, files = sandbox._sealable_absent_ceilings()

    assert "mcp-project-trust" in sandbox._CREW_READONLY_LEAVES
    assert "mcp-project-trust" in sandbox._CREW_PRECREATE_READONLY_DIR_LEAVES
    assert "mcp-project-trust" in sandbox._CREW_NOFOLLOW_READONLY_DIR_LEAVES
    assert "mcp-project-trust" not in sandbox._CREW_SANDBOX_VISIBLE_LEAVES
    # The DIRECTORY is the seal, so an absent grant file cannot be created either.
    assert str(tmp_path / "mcp-project-trust") in dirs
    assert not any(path.endswith("grants.json") for path in files)


def test_a_link_at_the_grant_dir_is_refused_before_the_sandbox_mount(tmp_path, _floor_monkeypatch):
    from kiro_crew import sandbox

    target = tmp_path / "mcp-project-trust"
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        target.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable on this platform")
    _floor_monkeypatch.setattr(sandbox, "_sealable_absent_ceilings", lambda: ([str(target)], []))
    with pytest.raises(sandbox.SandboxCeilingUnsealable):
        sandbox._materialize_sealable_ceilings()


def test_a_delegated_workspace_on_the_grant_dir_is_refused(tmp_path, _floor_monkeypatch):
    from kiro_crew import sandbox

    home = tmp_path / "crew"
    target = home / "mcp-project-trust"
    target.mkdir(parents=True)
    _floor_monkeypatch.setattr(sandbox, "config_dir", lambda: home)
    _floor_monkeypatch.setattr(sandbox, "_resolved_kiro_agents_targets", lambda: [])
    _floor_monkeypatch.setattr(sandbox.sys, "platform", "win32")
    reason = sandbox.delegated_workspace_exposes_sealed_target(str(target))
    assert reason is not None and "sealed project MCP consent" in reason


def test_a_linked_grant_dir_grants_nothing_and_refuses_writes(tmp_path):
    checkout = _checkout(tmp_path)
    store_dir = project_mcp_trust.store_path().parent
    store_dir.parent.mkdir(parents=True, exist_ok=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    try:
        store_dir.symlink_to(elsewhere, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable on this platform")
    key = os.path.realpath(checkout)
    (elsewhere / "grants.json").write_text(
        json.dumps(
            {
                "version": 1,
                "kind": "project-mcp",
                "granted": [
                    {
                        "path": key,
                        "identity": skill_trust._instance_identity(key),
                        "launch": _launch(checkout),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    project_mcp_trust.reset_cache_for_tests()
    assert _trusted(checkout) is False
    with pytest.raises(project_mcp_trust.TrustStoreUnreadable):
        project_mcp_trust.grant_project_mcp_trust(checkout)


# ── the grant is bound to the launch set the operator reviewed ──


def _rewrite_spec(checkout: Path, servers: dict, tools: list | None = None) -> None:
    (checkout / ".kiro" / "agents" / "helper.json").write_text(
        json.dumps(
            {
                "name": "kirocrew",
                "mcpServers": servers,
                "tools": tools or [f"@{n}" for n in servers],
            }
        ),
        encoding="utf-8",
    )


@pytest.mark.parametrize("backend", _MIRRORED)
def test_a_spec_edited_after_the_grant_is_not_trusted(backend, tmp_path):
    checkout = _checkout(tmp_path)
    project_mcp_trust.grant_project_mcp_trust(checkout)
    _rewrite_spec(
        checkout,
        {
            "repo-srv": {"command": "/nonexistent/project-mcp-trust/swapped", "args": []},
            "kirocrew-core": {"disabledTools": ["spawn_run"]},
        },
    )
    assert _trusted(checkout) is False
    servers = _array(backend, checkout)
    assert "/nonexistent/project-mcp-trust/swapped" not in _commands(servers)
    assert _REPO_CMD not in _commands(servers)


@pytest.mark.parametrize("backend", _MIRRORED)
def test_one_changed_server_withholds_every_project_server(backend, tmp_path):
    other = "/nonexistent/project-mcp-trust/other"
    checkout = _checkout(
        tmp_path,
        servers={"repo-srv": {"command": _REPO_CMD}, "other-srv": {"command": other}},
        tools=["@repo-srv", "@other-srv"],
    )
    project_mcp_trust.grant_project_mcp_trust(checkout)
    assert {_REPO_CMD, other} <= _commands(_array(backend, checkout)), "precondition"
    _rewrite_spec(
        checkout,
        {"repo-srv": {"command": _REPO_CMD}, "other-srv": {"command": other, "args": ["-x"]}},
    )
    commands = _commands(_array(backend, checkout))
    assert _REPO_CMD not in commands and other not in commands


@pytest.mark.parametrize(
    "edit",
    [
        {"cwd": "/tmp"},
        {"env": {"LD_PRELOAD": "/x.so"}},
        {"headers": {"Authorization": "Bearer x"}},
        {"type": "http"},
        {"url": "http://127.0.0.1:1/mcp"},
        {"args": ["--evil"]},
        {"someFutureLaunchKey": True},
    ],
    ids=["cwd", "env", "headers", "type", "url", "args", "unknown-key"],
)
def test_every_non_restriction_key_changes_the_fingerprint(edit):
    base = {"mcpServers": {"s": {"command": "/x"}}}
    changed = {"mcpServers": {"s": {"command": "/x", **edit}}}
    assert project_mcp_trust.launch_fingerprint(base) != project_mcp_trust.launch_fingerprint(
        changed
    )


@pytest.mark.parametrize("backend", _MIRRORED)
def test_a_switch_off_edit_never_re_prompts(backend, tmp_path):
    checkout = _checkout(tmp_path)
    project_mcp_trust.grant_project_mcp_trust(checkout)
    _rewrite_spec(
        checkout,
        {
            # Repo-srv is untouched: a per-tool narrowing withholds the whole server
            # on codex and goose by design, which is not a re-prompt.
            "repo-srv": {"command": _REPO_CMD, "args": []},
            "kirocrew-core": {"disabledTools": ["spawn_run", "cron_add"], "disabled": True},
        },
        tools=["@repo-srv", "@kirocrew-core"],
    )
    assert _trusted(checkout) is True
    assert _REPO_CMD in _commands(_array(backend, checkout))


@pytest.mark.parametrize("backend", _MIRRORED)
def test_a_matching_spec_is_trusted(backend, tmp_path):
    checkout = _checkout(tmp_path)
    project_mcp_trust.grant_project_mcp_trust(checkout)
    spec = json.loads((checkout / ".kiro" / "agents" / "helper.json").read_text())
    assert project_mcp_trust.is_project_trusted(checkout, "kirocrew", spec) is True
    assert project_mcp_trust.is_project_trusted(checkout, "other-agent", spec) is False
    assert _REPO_CMD in _commands(_array(backend, checkout))


def test_the_store_holds_hashes_never_env_or_header_values(tmp_path):
    checkout = _checkout(
        tmp_path,
        servers={
            "repo-srv": {
                "command": _REPO_CMD,
                "env": {"API_KEY": "sk-secret-value-1234"},
                "headers": {"Authorization": "Bearer header-secret-5678"},
            }
        },
        tools=["@repo-srv"],
    )
    project_mcp_trust.grant_project_mcp_trust(checkout)
    text = project_mcp_trust.store_path().read_text(encoding="utf-8")
    assert "sk-secret-value-1234" not in text and "header-secret-5678" not in text
    assert "API_KEY" not in text and _REPO_CMD not in text


def test_a_grant_for_a_launch_set_the_operator_did_not_see_is_refused(tmp_path):
    checkout = _checkout(tmp_path)
    seen = _launch(checkout)
    _rewrite_spec(checkout, {"repo-srv": {"command": "/nonexistent/project-mcp-trust/later"}})
    with pytest.raises(project_mcp_trust.ReviewedSpecChanged):
        project_mcp_trust.grant_project_mcp_trust(checkout, expected_launch=seen)
    assert not project_mcp_trust.store_path().exists()


def test_a_row_without_a_launch_map_grants_nothing(tmp_path):
    checkout = _checkout(tmp_path)
    key = os.path.realpath(checkout)
    _write_store(
        {
            "version": 1,
            "kind": "project-mcp",
            "granted": [{"path": key, "identity": skill_trust._instance_identity(key)}],
        }
    )
    assert _trusted(checkout) is False
    assert project_mcp_trust.list_mcp_trusted_projects()[0]["bound"] is False


def test_the_dialog_masks_env_and_header_values_and_bounds_names(tmp_path):
    long_name = "n" * 5000
    servers = {
        "repo-srv": {
            "command": _REPO_CMD,
            "args": ["--port", "1"],
            "env": {"API_KEY": "sk-secret-value-1234"},
            "headers": {"Authorization": "Bearer header-secret-5678"},
        },
        "remote": {"url": "https://user:pw@example.invalid/mcp?token=abc#frag"},
        long_name: {"command": "/x"},
    }
    servers.update({f"s{i:03d}": {"command": "/y"} for i in range(70)})
    checkout = _checkout(tmp_path, servers=servers, tools=["@repo-srv"])
    snap = handlers._snapshot(checkout)
    text = json.dumps(snap)
    assert "sk-secret-value-1234" not in text and "header-secret-5678" not in text
    assert "token=abc" not in text and "pw@" not in text
    rows = {row["name"]: row for row in snap["servers"]}
    assert rows["repo-srv"]["command"] == _REPO_CMD
    assert rows["repo-srv"]["args"] == ["--port", "1"]
    assert rows["repo-srv"]["env_keys"] == ["API_KEY"]
    assert rows["repo-srv"]["header_keys"] == ["Authorization"]
    assert rows["remote"]["url"] == "https://example.invalid/mcp"
    assert all(len(row["name"]) <= 256 for row in snap["servers"])
    assert len(snap["servers"]) == 64
    assert snap["servers_omitted"] == len(servers) - 64


@pytest.mark.asyncio
async def test_grant_refuses_a_spec_changed_since_review(tmp_path, slot_project):
    checkout = _checkout(tmp_path)
    slot_project["dir"] = checkout
    client = await _client(checkout)
    try:
        state = await (await client.get("/api/mcp/project-trust")).json()
        _rewrite_spec(checkout, {"repo-srv": {"command": "/nonexistent/project-mcp-trust/later"}})
        resp = await client.post(
            "/api/mcp/project-trust",
            json={"expected_key": state["project_key"], "expected_launch": state["launch"]},
        )
        assert resp.status == 409
        assert (await resp.json())["code"] == "mcp_trust_spec_changed"
    finally:
        await client.close()
    assert project_mcp_trust.list_mcp_trusted_projects() == []


def test_restriction_keys_never_change_the_fingerprint():
    base = {"mcpServers": {"s": {"command": "/x"}}}
    narrowed = {"mcpServers": {"s": {"command": "/x", "disabledTools": ["t"], "disabled": True}}}
    assert project_mcp_trust.launch_fingerprint(base) == project_mcp_trust.launch_fingerprint(
        narrowed
    )


# ── hooks are part of the launch set ──


def _write_spec(checkout: Path, spec: dict) -> None:
    (checkout / ".kiro" / "agents" / "helper.json").write_text(json.dumps(spec), encoding="utf-8")


def _spec_with_hooks(hooks: object) -> dict:
    return {
        "name": "kirocrew",
        "mcpServers": {"repo-srv": {"command": _REPO_CMD, "args": []}},
        "tools": ["@repo-srv"],
        "hooks": hooks,
    }


@pytest.mark.parametrize("backend", _MIRRORED)
def test_a_hook_added_after_the_grant_withholds_every_server(backend, tmp_path):
    checkout = _checkout(
        tmp_path, servers={"repo-srv": {"command": _REPO_CMD}}, tools=["@repo-srv"]
    )
    project_mcp_trust.grant_project_mcp_trust(checkout)
    assert _REPO_CMD in _commands(_array(backend, checkout)), "precondition"
    _write_spec(checkout, _spec_with_hooks({"agentSpawn": [{"command": "./scripts/x.sh"}]}))
    assert _trusted(checkout) is False
    assert _REPO_CMD not in _commands(_array(backend, checkout))


@pytest.mark.parametrize(
    "edit",
    [
        {"command": "./other.sh"},
        {"matcher": "execute_bash"},
        {"timeout_ms": 1},
        {"someFutureHookKey": 1},
    ],
    ids=["command", "matcher", "timeout_ms", "unknown-key"],
)
def test_every_hook_launch_key_changes_the_fingerprint(edit):
    base = _spec_with_hooks({"preToolUse": [{"command": "./x.sh"}]})
    changed = _spec_with_hooks({"preToolUse": [{"command": "./x.sh", **edit}]})
    assert project_mcp_trust.launch_fingerprint(base) != project_mcp_trust.launch_fingerprint(
        changed
    )


@pytest.mark.parametrize(
    "hooks",
    [{"preToolUse": [{"command": "./x.sh"}]}, [{"event": "preToolUse", "command": "./x.sh"}]],
    ids=["object-form", "array-form"],
)
def test_toggling_a_hook_enabled_never_re_prompts(hooks, tmp_path):
    checkout = tmp_path / "repo"
    (checkout / ".kiro" / "agents").mkdir(parents=True)
    _write_spec(checkout, _spec_with_hooks(hooks))
    project_mcp_trust.grant_project_mcp_trust(checkout)
    off = json.loads(json.dumps(hooks))
    (off["preToolUse"][0] if isinstance(off, dict) else off[0])["enabled"] = False
    _write_spec(checkout, _spec_with_hooks(off))
    assert _trusted(checkout) is True


def test_a_malformed_hooks_value_changes_the_fingerprint():
    assert project_mcp_trust.launch_fingerprint(
        _spec_with_hooks("not-a-hooks-value")
    ) != project_mcp_trust.launch_fingerprint(_spec_with_hooks({}))


def test_the_dialog_lists_hooks_with_env_values_masked(tmp_path):
    checkout = tmp_path / "repo"
    (checkout / ".kiro" / "agents").mkdir(parents=True)
    _write_spec(
        checkout,
        _spec_with_hooks(
            {
                "preToolUse": [
                    {
                        "command": "./scripts/check.sh",
                        "matcher": "execute_bash",
                        "env": {"HOOK_TOKEN": "hook-secret-9999"},
                    },
                    {"command": "./scripts/off.sh", "enabled": False},
                ]
            }
        ),
    )
    snap = handlers._snapshot(checkout)
    assert "hook-secret-9999" not in json.dumps(snap)
    first, second = snap["hooks"]
    assert first["event"] == "preToolUse" and first["command"] == "./scripts/check.sh"
    assert first["matcher"] == "execute_bash" and "env_keys" not in first
    # The object form has no off switch at runtime, so it is never shown as off.
    assert first["enabled"] is True and second["enabled"] is True
    assert snap["hooks_omitted"] == 0


def test_spec_hooks_follow_the_fingerprinted_grant(tmp_path):
    """The hooks reader asks the same verdict, on the spec it is about to run."""
    checkout = tmp_path / "repo"
    (checkout / ".kiro" / "agents").mkdir(parents=True)
    _write_spec(checkout, _spec_with_hooks({"agentSpawn": [{"command": "./scripts/x.sh"}]}))
    assert session_mcp.trusted_project_agent_spec("kirocrew", checkout) == (False, None)
    project_mcp_trust.grant_project_mcp_trust(checkout)
    declared, spec = session_mcp.trusted_project_agent_spec("kirocrew", checkout)
    assert declared is True and spec is not None
    _write_spec(checkout, _spec_with_hooks({"agentSpawn": [{"command": "./scripts/swapped.sh"}]}))
    assert session_mcp.trusted_project_agent_spec("kirocrew", checkout) == (False, None)


def test_the_dialog_shows_an_array_form_hooks_action_command(tmp_path):
    checkout = tmp_path / "repo"
    (checkout / ".kiro" / "agents").mkdir(parents=True)
    _write_spec(
        checkout,
        _spec_with_hooks(
            [
                {
                    "name": "h",
                    "trigger": "preToolUse",
                    "action": {
                        "type": "command",
                        "command": "./scripts/start.sh",
                        "env": {"A_TOKEN": "array-secret-4242"},
                    },
                }
            ]
        ),
    )
    snap = handlers._snapshot(checkout)
    (row,) = snap["hooks"]
    assert row["command"] == "./scripts/start.sh"
    assert row["action_type"] == "command"
    assert row["event"] == "preToolUse"
    assert "env_keys" not in row
    assert "array-secret-4242" not in json.dumps(snap)


def test_an_array_hook_shows_only_the_command_that_runs(tmp_path):
    checkout = tmp_path / "repo"
    (checkout / ".kiro" / "agents").mkdir(parents=True)
    _write_spec(
        checkout,
        _spec_with_hooks(
            [
                {
                    "trigger": "preToolUse",
                    "event": "decoy-event",
                    "command": "echo shown",
                    "env": {"DECOY": "x"},
                    "action": {"type": "command", "command": "echo executed"},
                }
            ]
        ),
    )
    (row,) = handlers._snapshot(checkout)["hooks"]
    assert row["command"] == "echo executed"
    assert row["event"] == "preToolUse"
    assert "env_keys" not in row


def test_an_object_hook_ignores_a_nested_action(tmp_path):
    checkout = tmp_path / "repo"
    (checkout / ".kiro" / "agents").mkdir(parents=True)
    _write_spec(
        checkout,
        _spec_with_hooks(
            {"preToolUse": [{"command": "echo runs", "action": {"command": "echo decoy"}}]}
        ),
    )
    (row,) = handlers._snapshot(checkout)["hooks"]
    assert row["command"] == "echo runs"


def test_a_non_string_url_shows_the_command_that_runs(tmp_path):
    checkout = _checkout(
        tmp_path,
        servers={"srv": {"url": ["https://decoy.invalid"], "command": "/opt/real", "args": ["a"]}},
        tools=["@srv"],
    )
    (row,) = handlers._snapshot(checkout)["servers"]
    assert row["url"] == ""
    assert row["command"] == "/opt/real"
    assert row["args"] == ["a"]


def test_a_string_url_wins_over_command_as_it_does_at_launch(tmp_path):
    checkout = _checkout(
        tmp_path,
        servers={"srv": {"url": "https://real.invalid/mcp", "command": "/opt/ignored"}},
        tools=["@srv"],
    )
    (row,) = handlers._snapshot(checkout)["servers"]
    assert row["url"] == "https://real.invalid/mcp"
    assert row["command"] == ""


def test_only_an_array_hook_can_show_as_off(tmp_path):
    checkout = tmp_path / "repo"
    (checkout / ".kiro" / "agents").mkdir(parents=True)
    _write_spec(
        checkout,
        _spec_with_hooks(
            [
                {
                    "trigger": "agentSpawn",
                    "enabled": False,
                    "action": {"type": "command", "command": "./x.sh"},
                }
            ]
        ),
    )
    (row,) = handlers._snapshot(checkout)["hooks"]
    assert row["enabled"] is False


def test_a_matcher_shows_only_on_tool_events(tmp_path):
    checkout = tmp_path / "repo"
    (checkout / ".kiro" / "agents").mkdir(parents=True)
    _write_spec(
        checkout,
        _spec_with_hooks(
            {
                "agentSpawn": [{"command": "./a.sh", "matcher": "execute_bash"}],
                "preToolUse": [{"command": "./b.sh", "matcher": "execute_bash"}],
            }
        ),
    )
    rows = {r["command"]: r for r in handlers._snapshot(checkout)["hooks"]}
    assert rows["./a.sh"]["matcher"] == ""
    assert rows["./b.sh"]["matcher"] == "execute_bash"
