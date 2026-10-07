"""The last run of a streamed Slack answer is settled when its message ends.

The live stream holds back the trailing run of each chunk until more text shows
where it ends. These tests drive the native handler and pin what happens to that
held run when the turn ends: clean text is sent, so the last word is not lost;
text that redacts differently as a whole is dropped and replaced by the final
copy. A run held before a tool call stays held until the text after it shows
where the run ends.
"""

from __future__ import annotations

import asyncio
import importlib

import pytest

from conftest import MockSlackClient
from kiro_crew.acp.client import AcpProcessDied
from kiro_crew.acp.types import EVENT_TOOL_CALL
from kiro_crew.providers.base import LLMEvent
from kiro_crew.slack.handler import handle_message

_handler_tests = importlib.import_module("test_slack_handler")
FakeSessionManager = _handler_tests.FakeSessionManager
FakeProvider = _handler_tests.FakeProvider


class _StreamingSlack(MockSlackClient):
    def __init__(self) -> None:
        super().__init__()
        self._stream_enabled = True


class _TrackSessions(FakeSessionManager):
    def __init__(self, provider) -> None:
        super().__init__(provider)
        self.calls = {"success": 0, "failure": 0}

    def record_success(self, key) -> None:
        self.calls["success"] += 1

    async def record_failure(self, key):
        self.calls["failure"] += 1
        return False


def _run(events: list[LLMEvent]) -> tuple[_StreamingSlack, _TrackSessions]:
    slack = _StreamingSlack()
    sessions = _TrackSessions(FakeProvider(events))
    asyncio.run(handle_message(slack, sessions, "C1", "q?", None, "msg1", "U1"))
    return slack, sessions


def _appends(slack: _StreamingSlack) -> list[tuple[str, str]]:
    return [
        (payload["ts"], payload["text"])
        for name, payload in slack.actions
        if name == "append_stream"
    ]


def _text(kind_text: str) -> LLMEvent:
    return LLMEvent(kind="text_chunk", text=kind_text)


def test_the_last_word_of_a_clean_answer_reaches_the_stream():
    slack, sessions = _run([_text("The answer is "), _text("forty2")])

    assert "".join(text for _ts, text in _appends(slack)) == "The answer is forty2"
    assert sessions.calls == {"success": 1, "failure": 0}


def test_a_one_word_answer_is_delivered_and_counted():
    slack, sessions = _run([_text("done")])

    assert [text for _ts, text in _appends(slack)] == ["done"]
    assert sessions.calls == {"success": 1, "failure": 0}


_OPAQUE = "q7Rk2LmZ9xWv4TbN8sYc1HdJ6pFe3GaU"


@pytest.mark.parametrize(
    "boundary",
    [
        LLMEvent(kind=EVENT_TOOL_CALL, title="Running: read", tool_name="read", tool_call_id="t1"),
        LLMEvent(kind=EVENT_TOOL_CALL, title="wait", tool_name="wait", tool_call_id="t-wait"),
    ],
    ids=["tool-card", "wait"],
)
def test_a_value_split_around_a_tool_call_is_filtered_as_one(boundary):
    slack, _sessions = _run(
        [_text("Header: Authorization: Bearer "), boundary, _text(f"{_OPAQUE} then more")]
    )

    streamed = "".join(text for _ts, text in _appends(slack))
    assert _OPAQUE not in streamed
    assert "[REDACTED: credential]" in streamed


def test_a_held_run_is_replaced_by_the_final_copy_when_the_whole_text_redacts():
    slack, _sessions = _run([_text("The access key is AKIA"), _text("IOSFODNN7"), _text("EXAMPLE")])

    streamed = "".join(text for _ts, text in _appends(slack))
    assert "IOSFODNN7EXAMPLE" not in streamed
    final = [
        payload.get("text", "") for name, payload in slack.actions if name in ("update", "post")
    ]
    assert any("[REDACTED: credential]" in text for text in final)


def _wait_call() -> LLMEvent:
    return LLMEvent(kind=EVENT_TOOL_CALL, title="wait", tool_name="wait", tool_call_id="t-wait")


def _every_text(slack: _StreamingSlack) -> str:
    return "\n".join(str(payload.get("text") or "") for _name, payload in slack.actions)


def test_a_value_split_by_a_wait_that_ends_the_turn_stays_filtered_in_the_final_copy():
    slack, _sessions = _run([_text("Header: Authorization: Bearer "), _wait_call(), _text(_OPAQUE)])

    assert _OPAQUE not in _every_text(slack)


def test_the_last_word_before_a_wait_that_ends_the_turn_is_posted():
    slack, sessions = _run([_text("first part 42"), _wait_call()])

    assert "42" in _every_text(slack)
    assert sessions.calls == {"success": 1, "failure": 0}


def _messages(slack: _StreamingSlack) -> list[str]:
    by_message: dict[str, str] = {}
    for ts, text in _appends(slack):
        by_message[ts] = by_message.get(ts, "") + text
    return list(by_message.values())


def test_the_word_held_at_a_wait_opens_the_next_message_after_a_break():
    # Intended: at a wait the stream cannot yet tell a whole word from the
    # start of a value the next text completes, and the filter must see a
    # value whole. So the held word moves to the next message; a break is
    # shown after it once the joined text has passed the filter.
    slack, sessions = _run([_text("Checking now 42"), _wait_call(), _text("Results are in.")])

    assert _messages(slack) == ["Checking now ", "42\n\nResults are in."]
    assert sessions.calls == {"success": 1, "failure": 0}


@pytest.mark.parametrize(
    ("before", "after", "hidden"),
    [
        ("Here it is key=AKI", "AIOSFODNN7EXAMPLE and more", "IOSFODNN7EXAMPLE"),
        ("bot 12345", "67890:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw then more", "AAHdqTcvCH1v"),
        ("see x", "AKIAIOSFODNN7EXAMPLE then more", "IOSFODNN7EXAMPLE"),
    ],
    ids=["after-a-name", "inside-digits", "no-break-inside-a-value"],
)
def test_a_value_split_by_a_wait_stays_filtered(before, after, hidden):
    slack, _sessions = _run([_text(before), _wait_call(), _text(after)])

    assert hidden not in _every_text(slack)


def test_a_key_split_after_its_prefix_by_a_wait_is_filtered_as_one():
    slack, _sessions = _run(
        [_text("The key is AKIA"), _wait_call(), _text("IOSFODNN7EXAMPLE and more")]
    )

    assert "IOSFODNN7EXAMPLE" not in _every_text(slack)


class _LastSendRefusedSlack(_StreamingSlack):
    """The first append lands; later appends are refused and no new stream opens."""

    def __init__(self) -> None:
        super().__init__()
        self._appends = 0
        self._starts = 0

    async def append_stream(self, channel, ts, text):
        self.actions.append(("append_stream", {"channel": channel, "ts": ts, "text": text}))
        self._appends += 1
        return self._appends == 1

    async def start_stream(self, channel, thread_ts, **kwargs):
        self._starts += 1
        if self._starts > 1:
            return None
        return await super().start_stream(channel, thread_ts, **kwargs)


def test_the_last_word_reaches_the_reader_when_its_send_and_retry_fail():
    slack = _LastSendRefusedSlack()
    sessions = _TrackSessions(FakeProvider([_text("The answer is "), _text("forty2")]))
    asyncio.run(handle_message(slack, sessions, "C1", "q?", None, "msg1", "U1"))

    updates = [payload.get("text", "") for name, payload in slack.actions if name == "update"]
    assert any("The answer is forty2" in text for text in updates)
    assert sessions.calls == {"success": 1, "failure": 0}


class _DiesAfterProvider(FakeProvider):
    """Yields its events, then the agent process dies before the turn completes."""

    async def stream(self, message, timeout=120.0):
        for event in self._events:
            yield event
        raise AcpProcessDied("gone")


def test_an_agent_that_dies_during_a_wait_shows_the_error_notice():
    slack = _StreamingSlack()
    sessions = _TrackSessions(_DiesAfterProvider([_text("first part 42"), _wait_call()]))
    asyncio.run(handle_message(slack, sessions, "C1", "q?", None, "msg1", "U1"))

    posts = [payload.get("text", "") for name, payload in slack.actions if name == "post"]
    assert any("Agent process died" in text for text in posts)


def test_a_key_split_by_a_wait_and_another_tool_call_is_filtered_as_one():
    tool = LLMEvent(
        kind=EVENT_TOOL_CALL, title="Running: read", tool_name="read", tool_call_id="t1"
    )
    slack, _sessions = _run(
        [_text("The key is AKIA"), _wait_call(), tool, _text("IOSFODNN7EXAMPLE and more")]
    )

    assert "IOSFODNN7EXAMPLE" not in _every_text(slack)
