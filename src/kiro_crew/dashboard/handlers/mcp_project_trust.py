"""Owner-only consent endpoints for a project's own MCP servers and hooks.

``/api/mcp/project-trust``: GET reads the requesting chat's state, the servers
its project would launch and every grant; POST grants consent for the requesting
chat's own project; DELETE withdraws a grant. All three are gated by
:func:`require_owner_dashboard_request` (403 ``owner_only``), because a grant lets
a checkout launch commands as the user at session start. The store is
:mod:`kiro_crew.project_mcp_trust`.
"""

from __future__ import annotations

import asyncio
import functools
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from aiohttp import web

from kiro_crew import project_mcp_trust as trust
from kiro_crew.dashboard.handlers._shared import (
    _read_session_key,
    requesting_slot_project,
    require_owner_dashboard_request,
)
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.executors import discovery_executor

#: Display bounds for the consent dialog. Every field it retains is cut, and
#: the count of servers past the cap is reported, never dropped silently. The
#: fingerprint covers the whole spec whatever the display shows.
_MAX_SERVERS = 64
_MAX_NAME_CHARS = 256
_MAX_COMMAND_CHARS = 1024
_MAX_ARGS = 32
_MAX_ARG_CHARS = 512
_MAX_KEYS = 32
_MAX_KEY_CHARS = 128
_ELLIPSIS = "\u2026"


def _cut(value: Any, limit: int) -> str:
    text = str(value)
    return text if len(text) <= limit else text[: limit - 1] + _ELLIPSIS


def _keys(value: Any) -> list[str]:
    """Names only: an ``env`` or ``headers`` VALUE can be a secret and is never shown."""
    if isinstance(value, dict):
        names = [str(k) for k in value]
    elif isinstance(value, list):
        names = [
            str(item.get("name")) for item in value if isinstance(item, dict) and item.get("name")
        ]
    else:
        return []
    return [_cut(n, _MAX_KEY_CHARS) for n in names[:_MAX_KEYS]]


def _display_url(value: Any) -> str:
    """The URL without query or fragment, which can carry a token."""
    try:
        parts = urlsplit(str(value))
        shown = urlunsplit((parts.scheme, parts.netloc.rpartition("@")[2], parts.path, "", ""))
    except ValueError:
        shown = ""
    return _cut(shown, _MAX_COMMAND_CHARS)


def _launch_servers(specs: list[tuple[str, dict[str, Any]]]) -> tuple[list[dict[str, Any]], int]:
    """The servers each spec would launch, display-bounded, and how many were omitted."""
    rows: list[dict[str, Any]] = []
    total = 0
    for agent, spec in specs:
        servers = spec.get("mcpServers")
        if not isinstance(servers, dict):
            continue
        for name, entry in servers.items():
            if not isinstance(entry, dict):
                continue
            # The same transport choice ``session_mcp.acp_server_element`` makes: a
            # non-empty string ``url`` wins, else a non-empty string ``command``.
            url = entry.get("url")
            url = url if isinstance(url, str) and url else ""
            command = entry.get("command")
            command = command if isinstance(command, str) and command else ""
            if not (url or command):
                continue
            total += 1
            if len(rows) >= _MAX_SERVERS:
                continue
            args = entry.get("args")
            arg_list = list(args) if isinstance(args, list) else []
            rows.append(
                {
                    "agent": _cut(agent, _MAX_NAME_CHARS),
                    "name": _cut(name, _MAX_NAME_CHARS),
                    "command": "" if url else _cut(command, _MAX_COMMAND_CHARS),
                    "args": [_cut(a, _MAX_ARG_CHARS) for a in arg_list[:_MAX_ARGS]],
                    "args_omitted": max(0, len(arg_list) - _MAX_ARGS),
                    "url": _display_url(url) if url else "",
                    "env_keys": _keys(entry.get("env")),
                    "header_keys": _keys(entry.get("headers")),
                }
            )
    return rows, total - len(rows)


_MAX_HOOKS = 64
_TOOL_EVENT_NAMES = frozenset({"pretooluse", "posttooluse"})


def _hook_rows(raw: Any) -> list[dict[str, Any]]:
    """What each hook runs, read where its runtime parser reads it.

    The object form (``spec_hooks._from_object_form``) keeps ``command`` and
    ``env`` on the entry under an event key. The array form
    (``spec_hooks._from_documents``) keeps them under ``action`` and names the
    event in ``trigger``; any top-level ``command`` there never runs, so it is
    never shown. Only the array form honours ``enabled: false``. No hook
    runtime applies an ``env`` key, so none is shown.
    """
    rows: list[dict[str, Any]] = []
    if isinstance(raw, dict):
        for event, entries in raw.items():
            if not isinstance(entries, list):
                continue
            for e in entries:
                if isinstance(e, dict):
                    rows.append(
                        {
                            "event": str(event),
                            "command": e.get("command"),
                            "action_type": "",
                            "matcher": e.get("matcher"),
                            # The object-form parser has no off switch: every
                            # entry runs, whatever an ``enabled`` key says.
                            "enabled": True,
                        }
                    )
    elif isinstance(raw, list):
        for doc in raw:
            if not isinstance(doc, dict):
                continue
            raw_action = doc.get("action")
            action: dict[str, Any] = raw_action if isinstance(raw_action, dict) else {}
            rows.append(
                {
                    "event": doc.get("trigger"),
                    "command": action.get("command"),
                    "action_type": action.get("type"),
                    "matcher": doc.get("matcher"),
                    "enabled": doc.get("enabled") is not False,
                }
            )
    return rows


def _text(value: Any, limit: int) -> str:
    """A string field for display; a non-string shows as nothing, never as a repr."""
    return _cut(value, limit) if isinstance(value, str) else ""


def _launch_hooks(specs: list[tuple[str, dict[str, Any]]]) -> tuple[list[dict[str, Any]], int]:
    """The hooks each spec would run, display-bounded, and how many were omitted.

    An off hook is listed too, marked, because turning it on does not re-ask.
    """
    rows: list[dict[str, Any]] = []
    total = 0
    for agent, spec in specs:
        for hook in _hook_rows(spec.get("hooks")):
            total += 1
            if len(rows) >= _MAX_HOOKS:
                continue
            rows.append(
                {
                    "agent": _cut(agent, _MAX_NAME_CHARS),
                    "event": _text(hook["event"], _MAX_NAME_CHARS),
                    "command": _text(hook["command"], _MAX_COMMAND_CHARS),
                    "action_type": _text(hook["action_type"], _MAX_NAME_CHARS),
                    # The runtime reads a matcher on the two tool events only.
                    "matcher": (
                        _text(hook["matcher"], _MAX_NAME_CHARS)
                        if str(hook["event"] or "").lower() in _TOOL_EVENT_NAMES
                        else ""
                    ),
                    "enabled": hook["enabled"],
                }
            )
    return rows, total - len(rows)


def _snapshot(project_dir: Path | None) -> dict[str, Any]:
    """Blocking read of MCP trust state for *project_dir* plus every grant.

    The specs are read ONCE here; the fingerprints the dialog echoes back and the
    servers it shows come from the same read.
    """
    project_key = trust.canonical_key(project_dir) if project_dir else None
    specs: list[tuple[str, dict[str, Any]]] = []
    too_many = False
    if project_dir is not None:
        try:
            specs = trust.project_agent_specs(project_dir)
        except ValueError:
            too_many = True
    launch = {name: trust.launch_fingerprint(spec) for name, spec in specs}
    servers, omitted = _launch_servers(specs)
    hooks, hooks_omitted = _launch_hooks(specs)
    return {
        "project": str(project_dir) if project_dir else "",
        "project_key": project_key or "",
        "trusted": bool(project_dir) and trust.is_launch_set_trusted(project_dir, launch),
        "servers": servers,
        "servers_omitted": omitted,
        "hooks": hooks,
        "hooks_omitted": hooks_omitted,
        "too_many_specs": too_many,
        "launch": launch,
        "grants": trust.list_mcp_trusted_projects(),
    }


async def api_mcp_project_trust(request: web.Request) -> web.Response:
    """GET: the requesting chat's project-MCP trust state and all grants."""
    denied = await require_owner_dashboard_request(request, "mcp_project_trust_read")
    if denied is not None:
        return denied
    state: DashboardState = request.app["state"]
    project_dir = requesting_slot_project(state, _read_session_key(request))
    snapshot = await asyncio.get_running_loop().run_in_executor(
        discovery_executor(), _snapshot, project_dir
    )
    return web.json_response(snapshot)


async def api_mcp_project_trust_grant(request: web.Request) -> web.Response:
    """POST: grant project-MCP trust to the REQUESTING CHAT's own project.

    The directory comes from the requesting slot, never from the body. The body
    carries ``expected_key`` and ``expected_launch`` -- the canonical identity and
    the launch fingerprints shown in the dialog. Both are required confirmations:
    a missing or mismatched value is refused, so consent is recorded only for the
    commands the operator saw.
    """
    denied = await require_owner_dashboard_request(request, "mcp_project_trust_grant")
    if denied is not None:
        return denied
    state: DashboardState = request.app["state"]
    session_key = _read_session_key(request)
    project_dir = requesting_slot_project(state, session_key)
    if project_dir is None:
        return web.json_response(
            {
                "error": "no project is set for this chat, so there is no directory to trust",
                "code": "mcp_trust_no_project",
            },
            status=400,
        )
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - a missing confirmation is refused below
        body = {}
    if not isinstance(body, dict):
        body = {}
    expected = body.get("expected_key")
    expected_launch = body.get("expected_launch")
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(
            discovery_executor(),
            functools.partial(
                trust.grant_project_mcp_trust,
                project_dir,
                expected_key=expected,
                expected_launch=expected_launch,
                session_key=session_key,
            ),
        )
    except trust.ReviewedProjectChanged:
        return web.json_response(
            {
                "error": (
                    "this chat's project is not the directory shown for review, "
                    "so consent was not recorded"
                ),
                "code": "mcp_trust_project_changed",
            },
            status=409,
        )
    except trust.ReviewedSpecChanged:
        return web.json_response(
            {
                "error": (
                    "this project's MCP servers changed after they were shown for "
                    "review, so consent was not recorded; review them again"
                ),
                "code": "mcp_trust_spec_changed",
            },
            status=409,
        )
    except ValueError as exc:
        return web.json_response(
            {"error": str(exc), "code": "mcp_trust_unusable_project"}, status=400
        )
    except trust.TrustStoreFull as exc:
        return web.json_response({"error": str(exc), "code": "mcp_trust_store_full"}, status=409)
    except trust.TrustStoreUnreadable as exc:
        return web.json_response(
            {"error": str(exc), "code": "mcp_trust_store_unreadable"}, status=409
        )
    snapshot = await loop.run_in_executor(discovery_executor(), _snapshot, project_dir)
    return web.json_response(snapshot)


async def api_mcp_project_trust_revoke(request: web.Request) -> web.Response:
    """DELETE: withdraw a project-MCP grant, by ``?path=`` or the chat's project.

    A caller-supplied path is safe here: removing trust only narrows. With no
    path, the chat's project resolves exactly as the grant resolves it.
    """
    denied = await require_owner_dashboard_request(request, "mcp_project_trust_revoke")
    if denied is not None:
        return denied
    state: DashboardState = request.app["state"]
    session_key = _read_session_key(request)
    project_dir = requesting_slot_project(state, session_key)
    target = request.query.get("path", "").strip()
    if not target:
        if project_dir is None:
            return web.json_response(
                {
                    "error": "no path given and no project is set for this chat",
                    "code": "mcp_trust_no_target",
                },
                status=400,
            )
        target = str(project_dir)
    loop = asyncio.get_running_loop()
    try:
        removed = await loop.run_in_executor(
            discovery_executor(),
            functools.partial(trust.revoke_project_mcp_trust, target, session_key=session_key),
        )
    except trust.TrustStoreUnreadable as exc:
        return web.json_response(
            {"error": str(exc), "code": "mcp_trust_store_unreadable"}, status=409
        )
    snapshot = await loop.run_in_executor(discovery_executor(), _snapshot, project_dir)
    snapshot["removed"] = removed
    return web.json_response(snapshot)
