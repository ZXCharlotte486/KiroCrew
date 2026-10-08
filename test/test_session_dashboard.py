"""A ROOT session's own dynamic dashboard: the slot-keyed read and the agent's write gate.

The property worth guarding hardest is WHO gets a page. Only a root session does -- one
no other session dispatched or adopted -- and the test for that is
``card_lifecycle.is_root_session``, not a new one. A worker's records belong to the
board of whoever dispatched it, so a dispatched or adopted session is answered "no page"
on the read and refused on the write.

The write gate is a security boundary: the page a caller writes is named by the
caller's OWN slot and never by anything it sends, and every gate the crew path has
(internal secret, operator switch, app denial, restricted mode, slot check) runs first.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.crew_log import session_tree_projection
from kiro_crew.dashboard.handlers import agent_panel as panel_routes
from kiro_crew.dashboard.handlers import member_dashboard as routes
from kiro_crew.dashboard_templates import catalog, instance

pytestmark = pytest.mark.asyncio

ROOT = "chat-100"
DISPATCHED = "chat-101"
ADOPTED = "chat-102"

PAGE = '<div><b data-dashboard-field="phase"></b></div>'


def _manifest(**over: Any) -> dict[str, Any]:
    raw = {
        "id": "fixture-board",
        "version": 1,
        "title": "Fixture board",
        "description": "A template these tests own.",
        "source": "builtin",
        "fields": {"phase": {"type": "string", "source": {"agentic": True}}},
    }
    raw.update(over)
    return raw


class _Node:
    def __init__(self, slot: str, parent_slot: str | None) -> None:
        self.slot = slot
        self.parent_slot = parent_slot
        self.cycle = False


SLOTS = {
    ROOT: SimpleNamespace(key=ROOT, _created_by="", agent="kiro"),
    # Dispatched through the session-control create verb: `_created_by` names the parent.
    DISPATCHED: SimpleNamespace(key=DISPATCHED, _created_by=ROOT, agent="kiro"),
    # Adopted: no `_created_by`, but the crew log's session tree holds a parent edge.
    ADOPTED: SimpleNamespace(key=ADOPTED, _created_by="", agent="kiro"),
}


class _Sessions:
    def has_session(self, _key: str) -> bool:
        return True

    def get_agent_selection(self, _key: str) -> tuple[str, str]:
        # An ordinary session selected a provider TEMPLATE, so it is bound to no crew.
        return "template", "kiro"

    def get_provider(self, _key: str) -> None:
        return None


class _State:
    def __init__(self) -> None:
        self.sessions = _Sessions()
        self.broadcasts: list[tuple[str, object]] = []

    def get_slot(self, name: str) -> Any:
        return SLOTS.get(name)

    def broadcast_ws(self, msg_type: str, data: object) -> None:
        self.broadcasts.append((msg_type, data))


async def _none(*_a: Any, **_k: Any) -> None:
    return None


@pytest.fixture(autouse=True)
def _env(tmp_path, _floor_monkeypatch):
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    _floor_monkeypatch.delenv("KIROCREW_CREW_LOG", raising=False)
    builtin = tmp_path / "builtin" / "fixture-board"
    builtin.mkdir(parents=True)
    (builtin / "manifest.json").write_text(json.dumps(_manifest()), encoding="utf-8")
    (builtin / "template.html").write_text(PAGE, encoding="utf-8")
    _floor_monkeypatch.setattr(catalog, "builtin_dir", lambda: builtin.parent)
    # The session tree, seeded: the adopted slot has a parent edge, the others none.
    nodes = {ADOPTED: _Node(ADOPTED, ROOT)}
    _floor_monkeypatch.setattr(
        session_tree_projection,
        "projection",
        lambda: SimpleNamespace(nodes=lambda: nodes, seeded_for_current_store=True),
    )
    _floor_monkeypatch.setattr(routes, "_deny_app_caller", _none)
    _floor_monkeypatch.setattr(routes, "_owner_only", _none)
    # The write gate's own vetting, opened: each is tested where it is defined, and
    # what is under test here is the root-session branch after them.
    _floor_monkeypatch.setattr(panel_routes, "_recognize_session", _none)
    _floor_monkeypatch.setattr(panel_routes, "_is_restricted_session", lambda *_a, **_k: False)
    _floor_monkeypatch.setattr(panel_routes, "_deny_app_caller", _none)
    _floor_monkeypatch.setattr(panel_routes.members_mod, "crew_panel_enabled", lambda: True)
    yield


@asynccontextmanager
async def _client(*, internal: bool = True):
    app = web.Application()
    app["state"] = _State()
    if internal:

        @web.middleware
        async def _internal(request, handler):
            request["internal_auth"] = True
            return await handler(request)

        app.middlewares.append(_internal)
    routes.register_member_dashboard_routes(app)
    panel_routes.register_agent_panel_routes(app)
    c = TestClient(TestServer(app))
    await c.start_server()
    try:
        yield c
    finally:
        await c.close()


def _read(slot: str) -> str:
    return f"/api/chat/slots/{slot}/dashboard"


def _as(slot: str) -> dict[str, str]:
    return {"X-Session-Key": f"dashboard:{slot}"}


# --------------------------------------------------------------------------
# the store key
# --------------------------------------------------------------------------


def test_a_session_key_names_its_own_directory_and_no_member_slug_can():
    key = instance.session_instance_key(ROOT)
    assert key == instance.session_instance_key(ROOT)
    assert key != instance.session_instance_key(DISPATCHED)
    assert instance.instance_dir(key).parent.name == "session-dashboards"
    # A key that did not come from the derivation is refused rather than joined.
    with pytest.raises(instance.InstanceError):
        instance.instance_dir("session:../../members/x")


# --------------------------------------------------------------------------
# the read the side panel does
# --------------------------------------------------------------------------


async def test_a_root_session_gets_its_page():
    instance.adopt(instance.session_instance_key(ROOT), "fixture-board")
    async with _client() as client:
        resp = await client.get(_read(ROOT))
        assert resp.status == 200, await resp.text()
        body = await resp.json()
        assert body["state"] == "live"
        assert body["template"] == {"id": "fixture-board", "version": 1}
        assert "rendered_html" in body


async def test_a_dispatched_session_gets_no_page():
    async with _client() as client:
        resp = await client.get(_read(DISPATCHED))
        assert resp.status == 404
        assert (await resp.json())["code"] == "not_root_session"


async def test_an_adopted_session_gets_no_page():
    async with _client() as client:
        resp = await client.get(_read(ADOPTED))
        assert resp.status == 404
        assert (await resp.json())["code"] == "not_root_session"


async def test_an_unknown_slot_is_a_404():
    async with _client() as client:
        resp = await client.get(_read("chat-nope"))
        assert resp.status == 404
        assert (await resp.json())["code"] == "slot_not_found"


async def test_the_slot_read_is_owner_only(monkeypatch):
    async def _deny(_request, _op):
        return web.json_response({"error": "owner only", "code": "owner_only"}, status=403)

    monkeypatch.setattr(routes, "_owner_only", _deny)
    async with _client() as client:
        resp = await client.get(_read(ROOT))
        assert resp.status == 403
        assert (await resp.json())["code"] == "owner_only"


async def test_the_slot_read_denies_an_app_caller(monkeypatch):
    async def _deny(_request, _op):
        return web.json_response({"error": "apps", "code": "app_denied"}, status=403)

    monkeypatch.setattr(routes, "_deny_app_caller", _deny)
    async with _client() as client:
        resp = await client.get(_read(ROOT))
        assert resp.status == 403


async def test_the_slot_read_is_redacted(tmp_path):
    # Assembled at runtime so the source holds no credential-shaped literal.
    token = "ghp" + "_" + "ZXAMPLEzxampleZXAMPLEzxampleZXAMPLE12"
    manifest = tmp_path / "builtin" / "fixture-board" / "manifest.json"
    manifest.write_text(json.dumps(_manifest(title=f"board {token}")), encoding="utf-8")
    instance.adopt(instance.session_instance_key(ROOT), "fixture-board")
    async with _client() as client:
        text = await (await client.get(_read(ROOT))).text()
    assert token not in text, "a credential in the page's manifest reached the reader"
    assert "board" in text


async def test_the_session_route_is_bound_through_the_deferred_binder():
    import inspect

    from kiro_crew.dashboard import server

    flat = " ".join(inspect.getsource(server._register_mcp_routes).split())
    assert '"/api/chat/slots/{slot}/dashboard", _deferred("member_dashboard",' in flat


# --------------------------------------------------------------------------
# the agent's write gate
# --------------------------------------------------------------------------


async def test_a_root_session_writes_its_own_page():
    async with _client() as client:
        staged = await client.post(
            "/api/agent-panel/dashboard/preview",
            json={"template_id": "fixture-board"},
            headers=_as(ROOT),
        )
        assert staged.status == 200, await staged.text()
        link = (await staged.json())["preview"]["preview_url"]
        assert link == f"/api/chat/slots/{ROOT}/dashboard?preview=1"
        applied = await client.post("/api/agent-panel/dashboard/apply", headers=_as(ROOT))
        assert applied.status == 200, await applied.text()
        # Landed on the caller's own store key, and the side panel's read sees it.
        assert instance.read(instance.session_instance_key(ROOT)).template_id == "fixture-board"
        body = await (await client.get(_read(ROOT))).json()
        assert body["template"]["id"] == "fixture-board"


@pytest.mark.parametrize("slot", [DISPATCHED, ADOPTED])
async def test_a_non_root_session_is_refused_the_write(slot):
    async with _client() as client:
        resp = await client.post(
            "/api/agent-panel/dashboard/preview",
            json={"template_id": "fixture-board"},
            headers=_as(slot),
        )
        assert resp.status == 403
        assert (await resp.json())["code"] == "not_root_session"
    assert instance.staged_preview(instance.session_instance_key(slot)) is None


async def test_a_subagent_with_no_slot_is_refused_the_write():
    async with _client() as client:
        resp = await client.get(
            "/api/agent-panel/dashboard/templates", headers=_as("subagent-of-chat-100")
        )
        assert resp.status == 400
        assert (await resp.json())["code"] == "no_dashboard_slot"


async def test_a_root_session_still_cannot_publish_a_crew_webview():
    """The widened branch is the dynamic dashboard's alone, not the crew panel's."""
    async with _client() as client:
        resp = await client.post(
            "/api/agent-panel/publish", json={"data": {"x": 1}}, headers=_as(ROOT)
        )
        assert resp.status == 400
        assert (await resp.json())["code"] == "no_crew"


async def test_the_write_gate_still_wants_the_internal_secret():
    async with _client(internal=False) as client:
        resp = await client.get("/api/agent-panel/dashboard/templates", headers=_as(ROOT))
        assert resp.status == 403
        assert (await resp.json())["code"] == "internal_secret_required"


async def test_an_unseeded_tree_refuses_the_write(monkeypatch):
    """`is_root_session` fails closed before the tree is seeded, and so does the gate."""
    monkeypatch.setattr(
        session_tree_projection,
        "projection",
        lambda: SimpleNamespace(nodes=lambda: {}, seeded_for_current_store=False),
    )
    monkeypatch.setattr("kiro_crew.dashboard.card_lifecycle._ask_for_lineage_seed", lambda: None)
    async with _client() as client:
        resp = await client.get("/api/agent-panel/dashboard/templates", headers=_as(ROOT))
        assert resp.status == 403
        assert (await resp.json())["code"] == "not_root_session"


# --------------------------------------------------------------------------
# whose page it is
# --------------------------------------------------------------------------


def _subject(rendered: str) -> str:
    """The ``subject`` the composed page hands its own script."""
    import re

    match = re.search(r'"subject":\s*"([a-z]+)"', rendered)
    assert match, "the composed page carries no subject"
    return match.group(1)


async def test_a_session_page_is_told_it_is_a_session():
    instance.adopt(instance.session_instance_key(ROOT), "fixture-board")
    async with _client() as client:
        body = await (await client.get(_read(ROOT))).json()
    assert _subject(body["rendered_html"]) == "session"


def test_a_crewmate_page_is_told_it_is_a_crewmate():
    from kiro_crew import dashboard_frame

    assert dashboard_frame.read_payload({})["subject"] == "crewmate"
    assert dashboard_frame.read_payload({}, subject="session")["subject"] == "session"
    # Anything else is a crewmate: the template's default wording.
    assert dashboard_frame.read_payload({}, subject="<b>")["subject"] == "crewmate"


def test_project_report_words_its_heading_for_a_session():
    """The shipped template switches its eyebrow on the subject, both ways."""
    from pathlib import Path

    import kiro_crew.dashboard_templates as templates_pkg

    real = Path(templates_pkg.__file__).parent / "builtin" / "project-report" / "template.html"
    page = real.read_text("utf-8")
    assert 'id="pr-eyebrow"' in page
    assert "'What this session is doing'" in page
    assert "'What this crewmate is doing'" in page
    assert ".subject === 'session'" in page
