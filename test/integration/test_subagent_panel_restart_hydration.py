"""The restart-level pin item 1 of #759 (PR #13443) could not place on itself.

PR #13443 rebuilds the Subagents panel from persisted run folders on a replaced
gateway. Every admission path on that rebuild requires
``state.get_slot(slot_key)`` to answer non-None -- a slot that does not exist
denies, by design, fail-closed. So recovery depends on an ORDERING: on a
replacement gateway a persisted run reappears only if its slot is live BEFORE
the reconnect replay runs. All 91 unit tests covering item 1
(``test_subagent_panel_durable_replay.py``) HAND-REGISTER their slots -- they
verify the frames and the records but never the real slot-seeding order, so if
hydration landed AFTER the replay the panel would come back empty on a cold
process and no unit test would notice (#13776).

This closes that gap the only way a unit test cannot: a real gateway RESTART
(a second boot on the same home) whose slot arrives through the production
seeding path -- ``restore_open_slots`` rehydrating ``open_slots.json`` during
boot -- rather than a hand-registered one.

The integration layer is the right home for it: ``gateway_boot`` boots the real
``GatewayOrchestrator.run()`` in this process, ``restart()`` is a genuine second
boot on the SAME ``KIROCREW_HOME`` with every home-bound global reset between
them exactly as a new process starts without them, and the live
``DashboardState`` is reachable for a direct assertion that the slot really was
seeded. The subprocess E2E route the issue sketches
(``spawn_feature_gateway().restart()``) proves the same thing across a process
boundary; this proves it a step cheaper and asserts the seam the unit tests
cannot -- ``get_slot`` non-None at replay time, produced by seeding and not by a
test hand-registering it.

The shape, from the issue's own recipe:

1. plant a dashboard slot's transcript on the home (one metadata line, one
   message) -- the production seeding input ``restore_open_slots`` reads;
2. plant that slot's key in ``open_slots.json`` and a persisted subagent run
   folder owned by that slot's session (the ``subagents/<id>/`` tree a finished
   persistent run leaves behind, as ``test_subagent_panel_durable_replay`` writes
   it);
3. ``restart()`` -- a genuine second boot on the same home, which does NOT
   re-seed, so those two files are the replacement boot's recovery input;
4. assert ``get_slot`` is non-None on the new boot (seeding ran), then subscribe
   the Subagents panel over the owner WebSocket and assert the persisted run
   replays as a ``subagent_done`` frame.

The negative control plants the same run under a slot with no ``open_slots.json``
entry: nothing restores it, ``get_slot`` answers None, and the fail-closed
``slot_missing`` gate withholds the frame -- so the positive case is the SEEDING
carrying the record through, not the record merely existing on disk.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import aiohttp
import pytest
from integration.conftest import booted_gateway

from kiro_crew.history import _safe_key
from kiro_crew.subagent_persistence import _subagents_dir

_PLANTED_RUN_ID = "restart-hydration-run"
_PLANTED_TASK = "audit the restart recovery order"
_PLANTED_AGENT = "kirocrew-worker"
# A dashboard slot the test owns end to end. Its transcript stem is
# ``dashboard_<slot>`` and its session key is ``dashboard:<slot>``; the planted
# run is owned by that session, and ``open_slots.json`` lists the slot key.
_SEEDED_SLOT = "restart-hydration-slot"
_UNSEEDED_SLOT = "no-such-slot-ever"


def _plant_persisted_run(*, agent_id: str, parent_session: str) -> None:
    """Write one finished persistent subagent run folder into the registry dir.

    The same on-disk shape ``test_subagent_panel_durable_replay.write_record``
    produces: a ``state.json`` identifying a persistent run owned by
    *parent_session*, plus a ``delivered`` ``tombstone.json`` so the reader
    classifies it as a completed run rather than waiting for a reconciler to
    stamp the ending. Written under ``subagent_persistence._subagents_dir()``,
    the registry the panel reader walks -- the rootdir conftest pins that to a
    per-test directory (``_isolate_subagents_dir``), which survives the restart
    because it is a module global the restart does not reset, so the replacement
    boot reads exactly this tree.
    """
    moment = time.time()
    folder = _subagents_dir() / agent_id
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "state.json").write_text(
        json.dumps(
            {
                "id": agent_id,
                "task": _PLANTED_TASK,
                "agent": _PLANTED_AGENT,
                "parent_session": parent_session,
                "started": moment - 120,
                "status": "running",
                "turns": 2,
                "memory_mode": "persistent",
                "execution_context": {"memory_mode": "persistent"},
                "app": "",
                "updated_at": moment - 60,
            }
        ),
        encoding="utf-8",
    )
    (folder / "tombstone.json").write_text(
        json.dumps(
            {
                "id": agent_id,
                "cause": "delivered",
                "recovery_action": "delivered",
                "started": moment - 120,
                "died": moment - 60,
            }
        ),
        encoding="utf-8",
    )


def _plant_open_slots(home: Path, slot_keys: list[str]) -> None:
    """Name *slot_keys* in ``open_slots.json`` so the next boot rehydrates them.

    ``restore_open_slots`` reads ``<home>/open_slots.json``'s ``keys`` list on
    every startup and rehydrates each from its session transcript -- the real
    slot-seeding path. Writing the file directly makes the restore deterministic
    rather than depending on a periodic-flush snapshot landing before restart.
    """
    (home / "open_slots.json").write_text(
        json.dumps({"keys": slot_keys, "ts": time.time()}), encoding="utf-8"
    )


def _plant_slot_transcript(home: Path, slot: str) -> None:
    """Write a minimal dashboard session transcript so boot can rehydrate *slot*.

    ``restore_open_slots`` reads each ``open_slots.json`` key's transcript from
    ``<home>/sessions/<safe_key>.jsonl`` and rebuilds the slot from its metadata
    -- the same shape the ``rich`` fixture ships for its seeded sessions. Written
    straight to disk (one metadata line, one user message) rather than through
    the store API: a store mutation on the event loop is refused under the
    integration layer's ``KIROCREW_STRICT_ON_LOOP_PERSIST`` guard, and the slot
    only has to exist on disk for the restart's restore to find it.
    """
    sessions = home / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    path = sessions / f"{_safe_key(f'dashboard:{slot}')}.jsonl"
    lines = [
        {
            "_type": "metadata",
            "created_at": "2026-01-15T09:00:00+00:00",
            "last_consolidated": 0,
            "closed": False,
            "title": "restart hydration slot",
            "agent": "default",
        },
        {
            "role": "user",
            "content": "hello from the pre-restart slot",
            "ts": "2026-01-15T09:00:05+00:00",
            "source_thread": "dashboard",
            "source_user": "dashboard",
        },
    ]
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")


async def _subscribe_subagents_replay(gw) -> list[dict]:
    """Open the owner WebSocket, subscribe the panel, collect the replay frames.

    The owner session cookie the boot minted (``gw._cookie``) is replayed on the
    ``/api/ws`` upgrade so the socket authenticates as the dashboard owner; the
    first ``slots`` frame proves it registered. ``subscribe_subagents`` then
    triggers the reconnect replay, whose frames (individually, or collapsed into
    one ``subagent_snapshot_batch`` above the batch threshold) are drained flat.
    """
    frames: list[dict] = []
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(
            f"{gw.base_url}/api/ws", headers={"Cookie": gw._cookie}
        ) as ws:
            first = await ws.receive_json(timeout=15)
            assert first.get("type") == "slots", f"owner WS did not register: {first!r}"
            await ws.send_json({"type": "subscribe_subagents"})
            # The replay is a burst right after the subscribe, so a short idle
            # gap means it is complete. Bounded so a silent socket fails here by
            # timeout rather than hanging the test.
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                try:
                    message = await ws.receive(timeout=3)
                except asyncio.TimeoutError:
                    break
                if message.type != aiohttp.WSMsgType.TEXT:
                    if message.type in (
                        aiohttp.WSMsgType.CLOSE,
                        aiohttp.WSMsgType.CLOSING,
                        aiohttp.WSMsgType.CLOSED,
                    ):
                        break
                    continue
                event = json.loads(message.data)
                if event.get("type") == "subagent_snapshot_batch":
                    frames.extend(event.get("data", {}).get("items", []))
                elif str(event.get("type", "")).startswith("subagent_"):
                    frames.append(event)
    return frames


@pytest.mark.asyncio
async def test_a_persisted_run_replays_after_its_slot_is_seeded_on_a_restart(integration_home):
    """The ordering the 91 unit tests cannot reach: real seeding, then replay.

    The slot's transcript, its ``open_slots.json`` entry and the persisted run
    folder are planted on the home BEFORE the first boot, so the production
    ``restore_open_slots`` seeds the slot on every boot and shutdown persists it
    (the slot is genuinely live, never pruned). The restart is then a true second
    boot whose ``restore_open_slots`` makes the slot live again -- before any
    ``subscribe_subagents`` replay reads it. A hydration-after-replay regression
    would leave ``get_slot`` None at replay time and drop the frame.
    """
    _plant_slot_transcript(integration_home, _SEEDED_SLOT)
    _plant_open_slots(integration_home, [_SEEDED_SLOT])
    _plant_persisted_run(
        agent_id=_PLANTED_RUN_ID,
        parent_session=f"dashboard:{_SEEDED_SLOT}",
    )

    async with booted_gateway(integration_home) as gw:
        # First boot already seeded the slot from the planted files; the restart
        # is the measured cold-start -- its restore seeds the slot again before
        # the replay below.
        assert gw.state.get_slot(_SEEDED_SLOT) is not None, "first boot did not seed the slot"
        await gw.restart()
        assert gw.state.get_slot(_SEEDED_SLOT) is not None, (
            "restore_open_slots did not rehydrate the slot on the replacement boot, "
            "so the replay below could never find it"
        )
        frames = await _subscribe_subagents_replay(gw)

    done = {
        str(frame.get("data", {}).get("id")): frame
        for frame in frames
        if frame.get("type") == "subagent_done"
    }
    assert _PLANTED_RUN_ID in done, (
        "the persisted run did not replay on the restarted gateway; its slot was "
        f"seeded from open_slots.json before the replay, so it should have. frames={frames!r}"
    )
    data = done[_PLANTED_RUN_ID]["data"]
    assert data["slot"] == _SEEDED_SLOT
    assert data["outcome"] == "completed"
    assert data["task"] == _PLANTED_TASK
    assert data["agent"] == _PLANTED_AGENT


@pytest.mark.asyncio
async def test_a_persisted_run_whose_slot_is_absent_is_withheld_on_a_restart(integration_home):
    """Negative control: without the seeded slot, the SAME record is dropped.

    Proves the positive test is the slot SEEDING carrying the record through, not
    the record merely existing on disk. The run is owned by a session with no
    ``open_slots.json`` entry, so no slot is restored for it; the replay's
    ``get_slot`` answers None and the fail-closed ``slot_missing`` gate withholds
    the frame. A real slot IS seeded for an unrelated session, so the panel is
    not merely empty -- the withholding is specific to this run's missing slot.
    """
    _plant_slot_transcript(integration_home, _SEEDED_SLOT)
    _plant_open_slots(integration_home, [_SEEDED_SLOT])
    _plant_persisted_run(
        agent_id=_PLANTED_RUN_ID,
        parent_session=f"dashboard:{_UNSEEDED_SLOT}",
    )

    async with booted_gateway(integration_home) as gw:
        await gw.restart()
        assert gw.state.get_slot(_SEEDED_SLOT) is not None, "the control slot was not seeded"
        assert gw.state.get_slot(_UNSEEDED_SLOT) is None, "the run's own slot should be absent"
        frames = await _subscribe_subagents_replay(gw)

    replayed = {
        str(frame.get("data", {}).get("id"))
        for frame in frames
        if frame.get("type") == "subagent_done"
    }
    assert _PLANTED_RUN_ID not in replayed, (
        "a persisted run whose owning slot was never seeded replayed anyway; the "
        f"restart's fail-closed slot gate should have withheld it. frames={frames!r}"
    )
