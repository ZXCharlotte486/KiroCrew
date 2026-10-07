"""Per-directory consent for a project's own MCP servers on mirrored sessions.

A project checkout's ``.kiro/agents/*.json`` can declare ``mcpServers``. On the
array-backed hosts (claude, codex, goose, opencode) every such server is a
command the adapter launches AS THE USER at ``session/new``, outside the tool
sandbox and before any prompt. Letting a cloned repository choose those
commands is running its author's code, so it needs the operator's explicit
consent. This module is that consent record.

It is deliberately a SEPARATE store from :mod:`kiro_crew.skill_trust`. A
project-skills grant consents to prose entering the agent's context; it never
consented to a command launching. Three things keep the two apart:

* a different file (``mcp-project-trust/grants.json``) with its own lock;
* a required ``"kind": "project-mcp"`` marker, so a skills store copied over
  this file parses to nothing;
* no shared enforcement call: :func:`is_project_trusted` reads only this file,
  and ``skills.project_skills_enabled`` plays no part in it.

What IS shared with ``skill_trust`` is the identity machinery, so the two
cannot drift on what "the same directory" means: the canonical key
(``realpath``, refused on platforms without no-follow traversal), the
instance identity a grant is bound to, and the membership token.

The store lives in its own data-home leaf, ``mcp-project-trust/``, which the OS
sandbox mounts read-only and the agent file gate write-protects -- the same seal
``mcp-launch-approvals`` carries (``sandbox._CREW_READONLY_LEAVES``). A grant
decides what the gateway launches outside the sandbox, and a directory identity
is plain ``stat`` output, so a store a sandboxed process could write would let
it consent for itself. Every writer runs in the gateway.

A grant is bound to the CONTENT the operator reviewed as well as the folder: each
row stores, per project agent, a fingerprint of that spec's launch set
(:func:`launch_fingerprint`). The project folder stays writable to the sandboxed
agent working in it, so a folder-only grant would let that agent swap in its own
command after the owner said yes. The launch set is the spec's ``mcpServers``
AND its ``hooks``: a trusted project's hooks run too. Any change to a launch field
re-asks; a change to a restriction key (``disabledTools``, ``disabled``, a hook's
``enabled``) does not. Only the hash is stored, never an ``env`` or ``headers``
value.

The fingerprint covers the spec text, not the files a command runs. The checkout
stays writable to the agent working in it, so a trusted project's scripts can
still be changed under the grant; the consent dialog says so in plain words.

The gate fails **closed** everywhere: a missing, unreadable or malformed store,
an unbound row, a directory whose instance changed, a launch set that differs
from the granted one, or an unsupported platform all mean "not trusted".
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from kiro_crew import platform_compat
from kiro_crew.agent_discovery import _read_agent_spec, project_agent_files, project_agent_name
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import config_dir
from kiro_crew.sel import sel
from kiro_crew.skill_trust import (
    ReviewedProjectChanged,
    TrustStoreFull,
    TrustStoreUnreadable,
    _as_epoch,
    _binding_token,
    _instance_identity,
    _store_signature,
    _StoreSignature,
    canonical_key,
)

__all__ = [
    "ReviewedProjectChanged",
    "ReviewedSpecChanged",
    "TrustStoreFull",
    "TrustStoreUnreadable",
    "canonical_key",
    "current_launch_fingerprints",
    "launch_fingerprint",
    "grant_project_mcp_trust",
    "is_launch_set_trusted",
    "is_project_trusted",
    "list_mcp_trusted_projects",
    "reset_cache_for_tests",
    "revoke_project_mcp_trust",
    "store_path",
]

logger = logging.getLogger(__name__)

#: Sealed read-only to sandboxed processes; see the module docstring.
STORE_DIR = "mcp-project-trust"
_STORE_FILENAME = "grants.json"
_SCHEMA_VERSION = 1
#: Required on every store this module reads. A ``project-skills.json`` has no
#: such field, so copying one over this file grants nothing.
_STORE_KIND = "project-mcp"
_MAX_GRANT_ENTRIES = 512
#: Project agent specs one grant fingerprints. A checkout declaring more is
#: refused at grant time rather than partly covered.
_MAX_AGENTS = 64
_MAX_AGENT_NAME_CHARS = 256
_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")
_FINGERPRINT_VERSION = 1
#: The only ``mcpServers`` entry keys that never reach a launch. Every other key,
#: including one added later, is part of the fingerprint, so an unknown key fails
#: closed by re-asking.
_RESTRICTION_KEYS = frozenset({"disabledTools", "disabled"})
#: The only hook entry key left out of the hash. On an array-form document it
#: turns the hook off or on, and the dialog lists an off hook too, so enabling it
#: later runs nothing unseen; the object form ignores it at runtime.
_HOOK_RESTRICTION_KEYS = frozenset({"enabled"})

#: ``(stat_signature, {binding token: {agent: fingerprint}})``.
_cache: tuple[_StoreSignature, dict[str, dict[str, str]]] | None = None


class ReviewedSpecChanged(RuntimeError):
    """The project's launch set is not the one the operator reviewed."""


_EXPECTED_KEY_UNSET = object()


def store_path() -> Path:
    """Absolute path of the MCP grant store."""
    return config_dir() / STORE_DIR / _STORE_FILENAME


def _strip(entry: Any, keys: frozenset[str]) -> Any:
    """*entry* without *keys* when it is an object; anything else as it is."""
    return {k: v for k, v in entry.items() if k not in keys} if isinstance(entry, dict) else entry


def _hooks_doc(raw: Any) -> Any:
    """``hooks`` with each entry's ``enabled`` removed, in both spec forms.

    The object form maps an event to a list of entries; the array form is a list
    of hook documents. Order is kept: hooks run in order. Any other shape is
    hashed as it is, so it fails closed.
    """
    if isinstance(raw, dict):
        return {
            str(event): (
                [_strip(e, _HOOK_RESTRICTION_KEYS) for e in entries]
                if isinstance(entries, list)
                else entries
            )
            for event, entries in raw.items()
        }
    if isinstance(raw, list):
        return [_strip(d, _HOOK_RESTRICTION_KEYS) for d in raw]
    return raw


def launch_fingerprint(spec: Any) -> str:
    """SHA-256 of *spec*'s launch set -- ``mcpServers`` and ``hooks`` -- minus switch-offs.

    Canonical JSON (sorted keys, no whitespace) over every server entry with
    ``disabledTools`` and ``disabled`` removed and every hook entry with
    ``enabled`` removed, so a switch-off never re-prompts while any launch field
    -- a server's ``command``, ``args``, ``url``, ``env``, ``headers``, ``cwd``,
    ``type``, a hook's ``command``, ``matcher``, ``timeout_ms``, or a key nobody
    has named yet -- changes the hash. A non-object shape is hashed as it is.
    """
    raw = spec.get("mcpServers") if isinstance(spec, dict) else None
    doc: dict[str, Any] = {"v": _FINGERPRINT_VERSION}
    if isinstance(raw, dict):
        doc["servers"] = {
            str(name): (
                _strip(entry, _RESTRICTION_KEYS) if isinstance(entry, dict) else {"entry": entry}
            )
            for name, entry in raw.items()
        }
    else:
        doc["raw"] = raw
    if isinstance(spec, dict) and "hooks" in spec:
        doc["hooks"] = _hooks_doc(spec.get("hooks"))
    text = json.dumps(doc, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=repr)
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


def project_agent_specs(project_dir: str | Path) -> list[tuple[str, dict[str, Any]]]:
    """``(agent name, spec)`` for each readable ``.kiro/agents/*.json`` in the checkout.

    Raises ``ValueError`` past :data:`_MAX_AGENTS`, so a grant never covers part
    of a checkout. An unreadable spec is skipped: the session read refuses it too.
    """
    out: list[tuple[str, dict[str, Any]]] = []
    for path in project_agent_files(project_dir, operation="project_mcp_trust", source="dashboard"):
        if path.suffix != ".json":
            continue
        spec = _read_agent_spec(path, operation="project_mcp_trust", source="dashboard")
        if not isinstance(spec, dict):
            continue
        name = project_agent_name(path)
        if not name or len(name) > _MAX_AGENT_NAME_CHARS:
            continue
        out.append((name, spec))
        if len(out) > _MAX_AGENTS:
            raise ValueError(f"project declares more than {_MAX_AGENTS} agent specs")
    return out


def current_launch_fingerprints(project_dir: str | Path) -> dict[str, str]:
    """``{agent name: launch fingerprint}`` for the checkout as it is now."""
    return {name: launch_fingerprint(spec) for name, spec in project_agent_specs(project_dir)}


def _valid_launch(value: Any) -> dict[str, str] | None:
    """A stored ``launch`` map, or ``None`` when its shape cannot be trusted."""
    if not isinstance(value, dict) or len(value) > _MAX_AGENTS:
        return None
    out: dict[str, str] = {}
    for name, fp in value.items():
        if not (isinstance(name, str) and 0 < len(name) <= _MAX_AGENT_NAME_CHARS):
            return None
        if not (isinstance(fp, str) and _FINGERPRINT_RE.match(fp)):
            return None
        out[name] = fp
    return out


def _store_is_linked() -> bool:
    """Whether the store's directory or file is a link or junction.

    The sandbox refuses to mount a linked leaf; the gateway refuses to read or
    write through one, so a link planted before the seal existed grants nothing.
    """
    path = store_path()
    try:
        return platform_compat.is_link_or_junction(
            path.parent
        ) or platform_compat.is_link_or_junction(path)
    except OSError:
        return True


def _store_dir() -> Path:
    """The store directory, created owner-only; a link there is refused, never followed."""
    directory = store_path().parent
    if platform_compat.is_link_or_junction(directory):
        raise TrustStoreUnreadable(f"{directory} is a link; refusing to use it")
    platform_compat.make_owner_only_dir(directory)
    platform_compat.restrict_dir_to_owner(directory)
    return directory


def _parse_tokens(text: str) -> dict[str, dict[str, str]]:
    """``{binding token: launch map}`` from store *text*; any malformed shape yields none."""
    try:
        data = json.loads(text)
    except ValueError as exc:
        logger.error("%s: not valid JSON (%s); ignoring every grant", _STORE_FILENAME, exc)
        return {}
    if not isinstance(data, dict):
        logger.error("%s: not a JSON object; ignoring every grant", _STORE_FILENAME)
        return {}
    if data.get("version") != _SCHEMA_VERSION or data.get("kind") != _STORE_KIND:
        logger.error(
            "%s: version %r / kind %r is not %d / %r; ignoring every grant",
            _STORE_FILENAME,
            data.get("version"),
            data.get("kind"),
            _SCHEMA_VERSION,
            _STORE_KIND,
        )
        return {}
    raw = data.get("granted")
    if not isinstance(raw, list):
        logger.error("%s: 'granted' is not an array; ignoring every grant", _STORE_FILENAME)
        return {}
    tokens: dict[str, dict[str, str]] = {}
    for entry in raw[:_MAX_GRANT_ENTRIES]:
        if not isinstance(entry, dict):
            continue
        path = entry.get("path")
        identity = entry.get("identity")
        launch = _valid_launch(entry.get("launch"))
        # A row without an identity cannot say WHICH directory was reviewed, and
        # one without a launch map cannot say WHAT was reviewed, so either grants
        # nothing (it is still listed, and so still revocable).
        if (
            isinstance(path, str)
            and path
            and os.path.isabs(path)
            and isinstance(identity, str)
            and identity
            and launch is not None
        ):
            tokens[_binding_token(path, identity)] = launch
    return tokens


def _read_tokens() -> dict[str, dict[str, str]]:
    """Enforcement map, cached on the store's stat signature; empty on any failure."""
    global _cache
    path = store_path()
    if _store_is_linked():
        logger.error("%s: the store is a link; ignoring every grant", path)
        _cache = None
        return {}
    signature = _store_signature(path)
    if signature is None:
        _cache = None
        return {}
    cached = _cache
    if cached is not None and cached[0] == signature:
        return cached[1]
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        logger.error("%s: unreadable (%s); ignoring every grant", _STORE_FILENAME, exc)
        _cache = None
        return {}
    tokens = _parse_tokens(text)
    _cache = (signature, tokens)
    return tokens


def _granted_launch_for_key(project_key: str | None) -> dict[str, str] | None:
    """The launch map a bound grant holds for an already-canonical key, else ``None``."""
    if not project_key:
        return None
    try:
        granted = _read_tokens()
        if not granted:
            return None
        identity = _instance_identity(project_key)
    except Exception:  # noqa: BLE001 - the gate fails closed, never raises
        logger.error("project MCP trust check failed; treating as untrusted", exc_info=True)
        return None
    if identity is None:
        return None
    return granted.get(_binding_token(project_key, identity))


def _granted_launch(project_dir: str | Path | None) -> dict[str, str] | None:
    """The launch map granted for *project_dir*; ``None`` for no grant or any doubt.

    ``None`` for a directory that is itself a link or junction: its target can be
    swapped after review, and the grant would follow it.
    """
    if project_dir is None:
        return None
    try:
        raw = str(project_dir).strip()
        if not raw or platform_compat.is_link_or_junction(os.path.expanduser(raw)):
            return None
        project_key = canonical_key(project_dir)
    except Exception:  # noqa: BLE001 - the gate fails closed, never raises
        return None
    return _granted_launch_for_key(project_key)


def is_project_trusted(project_dir: str | Path | None, agent: str | None, spec: Any) -> bool:
    """Whether *spec* -- the project spec for *agent*, as just read -- may launch.

    True only when *project_dir* holds a bound grant whose fingerprint for *agent*
    equals :func:`launch_fingerprint` of this very *spec*. The caller passes the
    spec it is about to project, so the verdict is about the bytes that will run,
    with no second read of the file. Never raises.
    """
    if not agent or not isinstance(spec, dict):
        return False
    launch = _granted_launch(project_dir)
    if launch is None:
        return False
    try:
        return launch.get(agent) == launch_fingerprint(spec)
    except Exception:  # noqa: BLE001 - the gate fails closed, never raises
        return False


def is_launch_set_trusted(project_dir: str | Path | None, current: dict[str, str]) -> bool:
    """Whether every agent in *current* matches the grant; for the dashboard's state."""
    launch = _granted_launch(project_dir)
    if launch is None or not current:
        return False
    return all(launch.get(name) == fp for name, fp in current.items())


@contextmanager
def _locked_store(*, exclusive: bool = True) -> Iterator[None]:
    """Hold this store's own lock across a read-modify-write."""
    try:
        lock_path = _store_dir() / (_STORE_FILENAME + ".lock")
        lock_path.touch(exist_ok=True)
        handle = open(lock_path, "r+")
    except OSError as exc:
        raise TrustStoreUnreadable(f"MCP trust store is not lockable: {exc}") from exc
    try:
        try:
            lock = platform_compat.file_lock(handle.fileno(), exclusive=exclusive)
        except OSError as exc:
            raise TrustStoreUnreadable(f"MCP trust store lock failed: {exc}") from exc
        with lock:
            yield
    finally:
        handle.close()


def _read_entries_unlocked() -> list[dict[str, Any]]:
    """Raw rows; ``[]`` only for an absent store, else raise on anything unreadable."""
    path = store_path()
    if _store_is_linked():
        raise TrustStoreUnreadable(f"{path} is a link; refusing to use it")
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise TrustStoreUnreadable(f"{_STORE_FILENAME} is unreadable: {exc}") from exc
    if not isinstance(data, dict):
        raise TrustStoreUnreadable(f"{_STORE_FILENAME} is not a JSON object")
    if data.get("version") != _SCHEMA_VERSION or data.get("kind") != _STORE_KIND:
        raise TrustStoreUnreadable(
            f"{_STORE_FILENAME} is not a version {_SCHEMA_VERSION} {_STORE_KIND!r} store; "
            "refusing to overwrite a store this build cannot read"
        )
    raw = data.get("granted")
    if not isinstance(raw, list) or any(not isinstance(e, dict) for e in raw):
        raise TrustStoreUnreadable(f"{_STORE_FILENAME} 'granted' is not an array of objects")
    return raw


def _write_entries_unlocked(entries: list[dict[str, Any]]) -> None:
    global _cache
    payload = {"version": _SCHEMA_VERSION, "kind": _STORE_KIND, "granted": entries}
    try:
        _store_dir()
        atomic_write(store_path(), json.dumps(payload, indent=2) + "\n", restrict_to_owner=True)
    except OSError as exc:
        raise TrustStoreUnreadable(f"{_STORE_FILENAME} is not writable: {exc}") from exc
    _cache = None


def grant_project_mcp_trust(
    project_dir: str | Path,
    *,
    expected_key: object = _EXPECTED_KEY_UNSET,
    expected_launch: object = _EXPECTED_KEY_UNSET,
    session_key: str = "",
) -> str:
    """Record MCP consent for *project_dir*'s current launch set; return its canonical key.

    The launch set is fingerprinted HERE, from the checkout as it is now. When
    *expected_launch* is supplied it is the ``{agent: fingerprint}`` map the
    dialog showed, and a checkout that changed since then raises
    ``ReviewedSpecChanged`` rather than recording consent for unseen commands.

    Raises ``ValueError`` for a path that cannot name a real, non-link
    directory, and ``ReviewedProjectChanged`` when *expected_key* (the identity
    shown to the operator) does not match the current directory. Audited with ``critical=True``
    before the write: consent that cannot be recorded is refused.
    """
    raw = str(project_dir).strip()
    if raw and platform_compat.is_link_or_junction(os.path.expanduser(raw)):
        raise ValueError(f"project directory is a link: {project_dir!r}")
    project_key = canonical_key(project_dir)
    if project_key is None:
        raise ValueError(f"not an existing absolute directory: {project_dir!r}")
    if expected_key is not _EXPECTED_KEY_UNSET and (
        not isinstance(expected_key, str) or expected_key != project_key
    ):
        raise ReviewedProjectChanged(str(expected_key or ""))
    identity = _instance_identity(project_key)
    if identity is None:
        raise ValueError(f"not a readable directory: {project_dir!r}")
    launch = current_launch_fingerprints(project_key)
    if not launch:
        raise ValueError(f"project declares no agent spec to trust: {project_dir!r}")
    if expected_launch is not _EXPECTED_KEY_UNSET and expected_launch != launch:
        raise ReviewedSpecChanged(project_key)
    with _locked_store():
        entries = _read_entries_unlocked()
        if any(
            e.get("path") == project_key
            and e.get("identity") == identity
            and e.get("launch") == launch
            for e in entries
        ):
            return project_key
        entries = [e for e in entries if e.get("path") != project_key]
        if len(entries) >= _MAX_GRANT_ENTRIES:
            raise TrustStoreFull(
                f"project MCP trust store is full ({_MAX_GRANT_ENTRIES} grants); "
                "revoke an existing grant before adding another"
            )
        sel().log_governance_decision(
            session_key=session_key,
            tool_name="project_mcp_trust",
            scope="project_mcp",
            item=project_key,
            outcome="allowed",
            rule="operator_granted_project_mcp",
            reason="operator granted project MCP server trust for this directory",
            critical=True,
        )
        entries.append(
            {
                "path": project_key,
                "identity": identity,
                "launch": launch,
                "granted_at": int(time.time()),
            }
        )
        _write_entries_unlocked(entries)
    return project_key


def revoke_project_mcp_trust(project_dir: str | Path, *, session_key: str = "") -> bool:
    """Withdraw MCP consent for *project_dir*. Returns whether a grant was removed.

    Matches the stored text first and resolves nothing for it, so a grant for a
    deleted or network path stays revocable without a filesystem lookup.
    """
    raw = str(project_dir).strip()
    expanded = os.path.expanduser(raw)
    project_key: str | None = None
    removed = False
    with _locked_store():
        entries = _read_entries_unlocked()
        exact = {c for c in (raw, expanded) if c}
        kept = [e for e in entries if e.get("path") not in exact]
        if len(kept) != len(entries):
            _write_entries_unlocked(kept)
            removed = True
    if not removed and not raw.replace("\\", "/").startswith("//"):
        project_key = canonical_key(project_dir)
        if project_key:
            with _locked_store():
                entries = _read_entries_unlocked()
                kept = [e for e in entries if e.get("path") != project_key]
                if len(kept) != len(entries):
                    _write_entries_unlocked(kept)
                    removed = True
    if removed:
        # A revoke narrows: an audit failure must not leave trust in place.
        try:
            sel().log_governance_decision(
                session_key=session_key,
                tool_name="project_mcp_trust",
                scope="project_mcp",
                item=project_key or raw,
                outcome="denied",
                rule="operator_revoked_project_mcp",
                reason="operator revoked project MCP server trust for this directory",
                critical=True,
            )
        except Exception:  # noqa: BLE001 - an unaudited revoke beats a blocked one
            logger.error("SEL audit failed for project MCP revoke; trust IS revoked", exc_info=True)
    return removed


def list_mcp_trusted_projects() -> list[dict[str, Any]]:
    """Every stored grant, newest first; an unreadable store lists none."""
    try:
        with _locked_store(exclusive=False):
            entries = _read_entries_unlocked()
    except TrustStoreUnreadable as exc:
        logger.error("%s; listing no grants", exc)
        return []
    rows: list[dict[str, Any]] = []
    for entry in entries:
        path = entry.get("path")
        if not isinstance(path, str) or not path:
            continue
        identity = entry.get("identity")
        rows.append(
            {
                "path": path,
                "granted_at": _as_epoch(entry.get("granted_at")),
                "exists": os.path.isdir(path),
                # Bound to both the folder instance and a launch set; an unbound
                # row is listed so it can be withdrawn, and grants nothing.
                "bound": bool(
                    isinstance(identity, str)
                    and identity
                    and _valid_launch(entry.get("launch")) is not None
                ),
            }
        )
    rows.sort(key=lambda r: r["granted_at"], reverse=True)
    return rows


def reset_cache_for_tests() -> None:
    """Drop the memoized enforcement read."""
    global _cache
    _cache = None
