"""The durable boundary a "New conversation" leaves behind.

A reset discards the native conversation and deliberately deletes nothing: the
slot stays open, its key is unchanged, and its transcript stays on disk. That is
the honest record of what happened -- and it is exactly why the boundary has to
be written down somewhere. Nothing in the slot's own files says where the
discarded conversation ends and the fresh one begins, so a pane reading the
transcript alone draws messages the model does not remember as current
context.

Four things carry that boundary, and each fails quietly rather than loudly:

**The member log's ``slot/reset``.** The one durable statement of where the
current conversation starts. Written only on a PERFORMED discard: an entry
written beside a refusal would move a boundary on a conversation that is still
whole.

**The roster fold.** The boundary reaches the pane through the projection the
roster read already serves, so it survives a reload and a gateway restart. A
fold that trusted its payload would let a damaged line hide an arbitrary prefix
of a live conversation.

**The discarded session's ``session/closed {discarded}``.** Nothing wrote this
reason before, so a discarded conversation's log ended mid-sentence and read as
open forever.

**That reason being NON-terminal.** A terminal close authorizes DELETING the
unit, and this is the one teardown whose contract is that the record survives
it -- the transcript is still on screen behind "Show earlier messages".
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from off_loop_helpers import off_loop

from kiro_crew import crew_log as lg
from kiro_crew import eventlog_hooks
from kiro_crew.crew_log import CrewLog, store
from kiro_crew.crew_log.schema import KIND_MEMBER
from kiro_crew.dashboard.chat_handlers import api_chat_slot_reset_conversation
from kiro_crew.dashboard.state import DashboardState, _ChatSlot
from kiro_crew.eventlog import types
from kiro_crew.eventlog.members_projections import RosterProjection
from kiro_crew.eventlog.service import get_service

DAY_MS = 86_400_000
#: A real member DM slot key: the owner is resolved from this shape alone.
DM_SLOT = "member-alice"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Every test writes into its own data home, never the live one."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    yield


# --------------------------------------------------------------------------- #
# the member log's slot/reset
# --------------------------------------------------------------------------- #
def _make_app(state: DashboardState) -> web.Application:
    app = web.Application()
    app["state"] = state

    @web.middleware
    async def _publish_app(request: web.Request, handler):
        request["app"] = ""  # a dashboard user, not an app token
        return await handler(request)

    app.middlewares.append(_publish_app)
    app.router.add_post(
        "/api/chat/slots/{slot}/reset-conversation", api_chat_slot_reset_conversation
    )
    return app


def _state(slot: _ChatSlot, *, performed: bool = True, mapped_sid: str = "") -> DashboardState:
    state = MagicMock(spec=DashboardState)
    state._slots = {slot.key: slot}
    state.sessions = MagicMock()
    state.sessions.discard_conversation = AsyncMock(return_value=performed)
    # No live provider: a slot restored from history, which is the case where the
    # sid citation has to come off the mapping. Returned as a real ``str``, since
    # a Mock here would be a truthy non-string the hook has to coerce.
    state.sessions.get_provider = MagicMock(return_value=None)
    state.sessions.mapped_sid = MagicMock(return_value=mapped_sid)
    state.subagents = None
    return state


def _dm_slot(*, running: bool = False, rows: int = 0, older: int = 0) -> _ChatSlot:
    slot = _ChatSlot(DM_SLOT)
    slot._app = ""
    if running:
        slot.task = MagicMock(done=MagicMock(return_value=False))
    for index in range(rows):
        slot.messages.append({"role": "user", "content": f"row {index}", "ts": ""})
    slot._disk_older_count = older
    return slot


async def _post(state: DashboardState, slot: str):
    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.post(
            f"/api/chat/slots/{slot}/reset-conversation", json={"replay": False}
        )
        return resp.status, await resp.json()


def _member_events(slug: str = "alice") -> list[dict]:
    """Every event in *slug*'s member log, after the queued appends have landed."""
    assert eventlog_hooks.drain_for_shutdown(10.0)
    path = store.crew_log_path(KIND_MEMBER, slug)
    if not path.exists():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if entry.get("type") != types.HEADER_TYPE:
            out.append(entry)
    return out


def _resets(slug: str = "alice") -> list[dict]:
    return [e for e in _member_events(slug) if e.get("type") == types.SLOT_RESET]


@pytest.mark.asyncio
async def test_a_performed_reset_records_the_boundary_on_the_members_log():
    """The entry a pane reads to know what the model still remembers.

    ``message_seq`` is the WHOLE transcript, both halves the detail read serves as
    one history: the rows below the in-memory window plus the window itself. A
    count of the window alone would place the boundary mid-conversation on any
    slot restored across a gateway restart, hiding nothing and revealing nothing
    the user asked about.
    """
    slot = _dm_slot(rows=3, older=7)
    state = _state(slot, mapped_sid="acp-discarded")

    status, body = await _post(state, DM_SLOT)

    assert status == 200
    assert body["reset"] is True
    (entry,) = _resets()
    assert entry["data"] == {
        "slot_key": DM_SLOT,
        "previous_sid": "acp-discarded",
        "message_seq": 10,
    }


@pytest.mark.asyncio
async def test_a_refused_reset_records_no_boundary():
    """A 409 left the conversation whole, so there is no boundary to record.

    The refusal under test is the ATOMIC one -- the discard's own ``skip_if_busy``
    declining after the route has committed -- because that is the one that
    happens past every pre-check, which is where an entry written too eagerly
    would land.
    """
    slot = _dm_slot()
    state = _state(slot, performed=False, mapped_sid="acp-still-here")

    status, body = await _post(state, DM_SLOT)

    assert status == 409
    assert body["code"] == "turn_in_flight"
    assert _resets() == []


@pytest.mark.asyncio
async def test_a_busy_slot_is_refused_before_the_teardown():
    """The pre-check the button's disabled state mirrors."""
    slot = _dm_slot(running=True)
    state = _state(slot)

    status, body = await _post(state, DM_SLOT)

    assert status == 409
    assert body["code"] == "turn_in_flight"
    state.sessions.discard_conversation.assert_not_awaited()
    assert _resets() == []


@pytest.mark.asyncio
async def test_a_slot_with_no_member_owner_records_nothing():
    """An ordinary chat slot's reset still works and writes no member event.

    The owner is resolved from the slot KEY, so a key that names no member names
    no log to write into -- and guessing one would put a boundary into a
    crewmate's record for a conversation that was never theirs.
    """
    slot = _ChatSlot("chat-1-foo")
    slot._app = ""
    state = _state(slot, mapped_sid="acp-ordinary")
    state._slots = {"chat-1-foo": slot}

    status, body = await _post(state, "chat-1-foo")

    assert status == 200
    assert body["reset"] is True
    assert _member_events() == []


def test_the_hook_coerces_a_payload_it_cannot_write():
    """The log is append-only, so a bad value would be a permanent line.

    The route's sid reads can both answer something that is not a string (a
    session manager with no mapping, a provider whose id was never set), and the
    honest value for "no id we can name" is the empty string rather than whatever
    object the probe returned.
    """
    assert eventlog_hooks.record_slot_reset(DM_SLOT, None, True) is True  # type: ignore[arg-type]

    (entry,) = _resets()
    assert entry["data"] == {"slot_key": DM_SLOT, "previous_sid": "", "message_seq": 0}


# --------------------------------------------------------------------------- #
# the roster fold
# --------------------------------------------------------------------------- #
def _event(slot_key, seq, ts, *, etype=types.SLOT_RESET):
    return {
        "type": etype,
        "seq": 1,
        "time": ts,
        "data": {"slot_key": slot_key, "previous_sid": "acp-old", "message_seq": seq},
    }


def _fold(*events):
    unit = RosterProjection()
    state = unit.init()
    for event in events:
        state = unit.apply(state, event)
    return unit.view(state)


def test_the_roster_fold_carries_the_current_conversations_start():
    view = _fold(_event(DM_SLOT, 10, 1_700_000_000_000))

    assert view["conversation_starts"] == {DM_SLOT: {"message_seq": 10, "ts": 1_700_000_000_000}}


def test_a_second_reset_supersedes_the_first_for_that_slot():
    """ "The CURRENT conversation starts here" is one answer per slot.

    Keeping both would need the reader to decide which is current, and the
    superseded boundary is already in the log for anybody asking when it happened.
    """
    view = _fold(
        _event(DM_SLOT, 10, 1_700_000_000_000),
        _event(DM_SLOT, 24, 1_700_000_900_000),
        _event("member-bob", 3, 1_700_000_500_000),
    )

    assert view["conversation_starts"][DM_SLOT] == {
        "message_seq": 24,
        "ts": 1_700_000_900_000,
    }
    assert view["conversation_starts"]["member-bob"] == {
        "message_seq": 3,
        "ts": 1_700_000_500_000,
    }


@pytest.mark.parametrize(
    "data",
    [
        pytest.param({"slot_key": "", "message_seq": 4}, id="no-slot-key"),
        pytest.param({"slot_key": 7, "message_seq": 4}, id="slot-key-not-a-string"),
        pytest.param({"slot_key": DM_SLOT}, id="no-message-seq"),
        pytest.param({"slot_key": DM_SLOT, "message_seq": "4"}, id="message-seq-not-a-number"),
        pytest.param({"slot_key": DM_SLOT, "message_seq": True}, id="message-seq-a-flag"),
        pytest.param({"slot_key": DM_SLOT, "message_seq": -1}, id="message-seq-negative"),
    ],
)
def test_a_boundary_the_fold_cannot_stand_behind_is_dropped(data):
    """These bytes come off a file the fold does not control.

    A damaged committed line, or an edit inside the member's own log directory,
    is exactly the input the writer's own clamp never saw -- and the value is
    served to a browser on every roster read, where a bad boundary hides an
    arbitrary prefix of a conversation the model does remember.
    """
    unit = RosterProjection()
    state = unit.init()

    after = unit.apply(state, {"type": types.SLOT_RESET, "seq": 1, "time": 1, "data": data})

    # The SAME object back, which is how the registry reads "nothing moved" and
    # why a dropped line publishes no frame.
    assert after is state
    assert "conversation_starts" not in unit.view(state)


def test_an_unreadable_time_is_dropped_rather_than_folded_as_zero():
    """A boundary with no moment is not a boundary: at 0 it hides nothing, and
    the pane would report a reset that it cannot place."""
    unit = RosterProjection()
    state = unit.init()
    event = _event(DM_SLOT, 4, None)

    assert unit.apply(state, event) is state


def test_the_boundary_map_is_bounded_and_evicts_the_oldest():
    """One key per slot read off an event, in an append-only log with no
    compaction, served on every roster read. Without a ceiling that is a row
    that grows for the life of the store."""
    from kiro_crew.eventlog.members_projections import _CONVERSATION_STARTS_MAX

    over = _CONVERSATION_STARTS_MAX + 3
    # Newest last, so the first three are the oldest and are the ones to go.
    view = _fold(*[_event(f"member-{n:02d}", n, 1_700_000_000_000 + n) for n in range(over)])

    starts = view["conversation_starts"]
    assert len(starts) == _CONVERSATION_STARTS_MAX
    assert "member-00" not in starts
    assert "member-02" not in starts
    assert f"member-{over - 1:02d}" in starts


def test_the_roster_folds_version_moved_with_the_new_field():
    """A savepoint written by a fold with no ``conversation_starts`` holds no
    boundary at all, and a resumed fold applies only the events past its
    watermark -- so that member's pane would show a discarded conversation as
    current for the life of the store. The version bump is what discards such a
    file; leaving it at 2 is the regression this pins."""
    assert RosterProjection.state_version == 3


def test_a_log_whose_only_event_is_a_reset_folds_to_the_boundary():
    """The cold-fold path the version bump sends every old savepoint down."""
    svc = get_service()
    svc.ensure("alice", "Alice")
    svc.append(
        "alice",
        types.SLOT_RESET,
        {"slot_key": DM_SLOT, "previous_sid": "acp-old", "message_seq": 12},
    )

    roster = get_service().snapshot("alice")["values"][types.PROJ_ROSTER]

    assert roster["conversation_starts"][DM_SLOT]["message_seq"] == 12
    assert roster["conversation_starts"][DM_SLOT]["ts"] > 0


# --------------------------------------------------------------------------- #
# the discarded session's own log
# --------------------------------------------------------------------------- #
def _provider_factory_reporting(session_id: str):
    """A provider whose mock reports *session_id* as a real ``str``.

    ``session_id_of`` requires one, so a bare ``AsyncMock`` attribute makes every
    emitter call a no-op and the test would pass by writing nothing at all.
    """

    def factory(session_key=None, agent=None, channel_id=None, **kwargs):
        provider = AsyncMock()
        provider.start = AsyncMock()
        provider.shutdown = AsyncMock()
        provider.is_process_alive = lambda: True
        provider.context_usage_pct = lambda: 0.0
        provider.context_window_tokens = lambda: 0
        provider.has_active_turn = lambda: False
        provider.runtime_abort_target = lambda: None
        provider.session_id = session_id
        return provider

    return factory


def _unit_path(unit_id: str) -> Path:
    return store.crew_log_path(lg.KIND_SESSION, unit_id)


def _closes(unit_id: str) -> list[dict]:
    path = _unit_path(unit_id)
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if entry.get("type") == "session/closed":
            out.append(entry)
    return out


@pytest.mark.asyncio
async def test_discarding_a_conversation_records_its_teardown(monkeypatch):
    """Driven through the REAL ``discard_conversation``, not a stub.

    A mocked teardown is how the missing entry stayed invisible: the route's own
    tests replace the session manager, so the only thing that can prove the close
    is written is the manager itself.
    """
    from kiro_crew.config import KiroCrewConfig
    from kiro_crew.crew_log import emit
    from kiro_crew.session import SessionManager

    monkeypatch.setenv(emit.CREW_LOG_ENV, "1")
    emit.reset_caches()
    try:
        cfg = KiroCrewConfig()
        manager = SessionManager(cfg, provider_factory=_provider_factory_reporting("acp-discarded"))
        await manager.get_or_create("thread-reset")
        emit.on_session_opened("acp-discarded", agent="kirocrew", slot="thread-reset")
        assert emit.flush(timeout=5.0)

        assert await manager.discard_conversation("thread-reset", replay=False) is True
        assert emit.flush(timeout=5.0)

        assert [e["data"]["reason"] for e in _closes("acp-discarded")] == ["discarded"]
    finally:
        emit.reset_caches()


def test_discarded_is_deliberately_not_a_terminal_close_reason():
    """The set is the authorization to DELETE a unit, not a resumability verdict.

    A discard does clear the sid unconditionally, so the id is genuinely
    unresumable and the intuitive reading would admit it. What that would also do
    is expire the log of a conversation still on screen behind "Show earlier
    messages": the transcript is deliberately kept, so the record outlives the
    context.
    """
    log = CrewLog.create(lg.KIND_SESSION, "acp-aged-discard", owner="qa", agent="kirocrew")
    from crew_log_type_helpers import minimal_data

    log.append(
        "session/opened",
        {**minimal_data(lg.KIND_SESSION, "session/opened"), "resumed": False},
        src="gateway",
    )
    log.append("session/closed", {"reason": "discarded"}, src="gateway")
    # Age the close past any retention the sweep could be asked for.
    path = _unit_path("acp-aged-discard")
    lines = path.read_text(encoding="utf-8").splitlines()
    for index in range(len(lines) - 1, -1, -1):
        entry = json.loads(lines[index])
        if entry.get("type") == "session/closed":
            entry["time"] = store.now_ms() - 400 * DAY_MS
            lines[index] = json.dumps(entry, separators=(",", ":"), sort_keys=True)
            break
    else:  # pragma: no cover - the append above already proved it is there
        raise AssertionError("no close to age")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    del log

    assert off_loop(store.sweep_expired, 30) == (0, 0)
    assert CrewLog.exists(lg.KIND_SESSION, "acp-aged-discard")


# --------------------------------------------------------------------------- #
# the successor's citation
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_the_successor_cites_the_discarded_session(monkeypatch):
    """The chain the boundary is read against, in the two steps that make it.

    A discard empties the key's ``sid`` and STASHES it as ``discarded_sid``,
    which is what keeps the id nameable: ``mapped_sid`` answers the stash, and
    that is the value the next allocation hands its ``session/opened``. Reading
    the resumable id instead would answer nothing here -- the whole point of a
    discard is that the id is not resumable -- and the successor would claim to
    be the slot's first conversation.
    """
    from kiro_crew.config import KiroCrewConfig
    from kiro_crew.crew_log import emit
    from kiro_crew.session import SessionManager

    monkeypatch.setenv(emit.CREW_LOG_ENV, "1")
    emit.reset_caches()
    try:
        cfg = KiroCrewConfig()
        manager = SessionManager(cfg, provider_factory=_provider_factory_reporting("acp-first"))
        await manager.get_or_create("thread-chain")
        # Mapped explicitly: the ACP provider maps its own id on a real
        # allocation, and the mock here is not one. The mapping is the INPUT to
        # this test, not its subject -- what is under test is what a discard
        # leaves behind in it.
        manager._session_map.set("thread-chain", "acp-first", provider="acp", cwd="")
        emit.on_session_opened("acp-first", agent="kirocrew", slot="thread-chain")
        assert emit.flush(timeout=5.0)
        assert await manager.discard_conversation("thread-chain", replay=False) is True

        # Step one: the id is gone as a resume target and still nameable.
        previous = manager.mapped_sid("thread-chain")
        assert previous == "acp-first"

        # Step two: the successor's own opening entry cites it.
        emit.on_session_opened(
            "acp-second", agent="kirocrew", slot="thread-chain", previous_sid=previous
        )
        assert emit.flush(timeout=5.0)

        opened = [
            json.loads(line)
            for line in _unit_path("acp-second").read_text(encoding="utf-8").splitlines()
            if json.loads(line).get("type") == "session/opened"
        ]
        assert [e["data"].get("previous", {}).get("sid") for e in opened] == ["acp-first"]
    finally:
        emit.reset_caches()


def test_slot_reset_is_in_the_logs_own_vocabulary():
    """``MemberLog.append`` refuses a type the vocabulary does not declare, and
    ``slot`` is a RESERVED namespace -- so a type registered in one place and not
    the other is refused as a typo'd built-in rather than written."""
    assert types.SLOT_RESET in types.ALL_EVENT_TYPES
    assert types.is_known_event_type(types.SLOT_RESET)
    assert not types.is_contributed_event_type(types.SLOT_RESET)


def test_the_hook_resolves_the_owner_from_the_slot_key_alone():
    """Including the ``.memory-<store>`` suffix a V2 member's DM key carries,
    which is part of the SLOT's identity and never of the slug."""
    assert eventlog_hooks.record_slot_reset("member-alice.memory-notes", "acp-x", 4) is True

    # Written into "alice"'s log, not into a log named for the suffixed key.
    (entry,) = _resets("alice")
    assert entry["data"]["slot_key"] == "member-alice.memory-notes"
