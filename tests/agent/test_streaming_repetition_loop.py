"""Regression tests for issue #127234: a streaming repetition loop never ends.

Every repetition check on main runs only after the stream ends. On an
uncapped endpoint (LM Studio, custom local server) a verbatim loop streams
forever. The fix applies the stop path's own criterion
(``is_runaway_repetition`` at >= 16k chars, then at every doubling, on a
bounded 64k tail) while the stream is live, on raw content and reasoning,
for both the chat-completions and Anthropic Messages wires. On a hit the
stream is closed on its owner thread and the turn ends through the existing
"Response Stopped — Repetition Detected" notice; the loop is neither
persisted nor continued.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent.repetition_guard import (
    STREAM_REPETITION_MIN_CHARS,
    STREAM_REPETITION_TAIL_CHARS,
    STOP_PATH_MIN_CHARS,
    StreamingRepetitionMonitor,
    is_runaway_repetition,
    live_repetition_tail,
)
from run_agent import AIAgent

_ISSUE_UNIT = "The backlog item is pending review. "


def _make_stream_chunk(content=None, reasoning_content=None, finish_reason=None, model=None):
    delta = SimpleNamespace(
        content=content,
        tool_calls=None,
        reasoning_content=reasoning_content,
        reasoning=None,
    )
    choice = SimpleNamespace(index=0, delta=delta, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice], model=model, usage=None)


class _TrackingStream:
    """Finite stand-in for an infinite loop: yields ``n_chunks`` loop deltas.

    Counts how many chunks the consumer pulled and records ``close()`` so the
    test proves the consumer stopped early instead of draining the stream.
    """

    def __init__(self, unit, n_chunks, *, channel="content"):
        self.unit = unit
        self.n_chunks = n_chunks
        self.channel = channel
        self.yielded = 0
        self.closed = False

    def __iter__(self):
        return self

    def __next__(self):
        if self.closed:
            raise StopIteration
        if self.yielded >= self.n_chunks:
            raise StopIteration
        self.yielded += 1
        if self.channel == "content":
            return _make_stream_chunk(content=self.unit)
        return _make_stream_chunk(reasoning_content=self.unit)

    def close(self):
        self.closed = True


def _streaming_agent(**kwargs):
    with (
        patch("model_tools.get_tool_definitions", return_value=[]),
        patch("model_tools.check_toolset_requirements", return_value={}),
    ):
        agent = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            **kwargs,
        )
        agent.api_mode = "chat_completions"
        agent._interrupt_requested = False
        return agent


def _run_stream(agent, tracking):
    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = tracking
    with patch.object(
        agent, "_create_request_openai_client", return_value=mock_client
    ):
        return agent._interruptible_streaming_api_call({})


class TestLiveMonitor:
    def test_thresholds_start_at_stop_path_scale_and_double(self):
        monitor = StreamingRepetitionMonitor()
        assert monitor.next_content_check == STOP_PATH_MIN_CHARS == 16_000
        assert monitor.next_reasoning_check == STOP_PATH_MIN_CHARS
        assert STREAM_REPETITION_MIN_CHARS == STOP_PATH_MIN_CHARS
        assert STREAM_REPETITION_TAIL_CHARS == 64_000

        assert monitor.add_content(15_999) is False
        assert monitor.add_content(1) is True
        monitor.advance_content()
        assert monitor.next_content_check == 32_000

        assert monitor.add_reasoning(16_000) is True
        monitor.advance_reasoning()
        assert monitor.next_reasoning_check == 32_000

    def test_tail_is_bounded(self):
        assert live_repetition_tail("abc") == "abc"
        long_text = "x" * (STREAM_REPETITION_TAIL_CHARS + 100)
        tail = live_repetition_tail(long_text)
        assert len(tail) == STREAM_REPETITION_TAIL_CHARS
        assert tail == long_text[-STREAM_REPETITION_TAIL_CHARS:]

    def test_issue_fixture_is_runaway_at_first_check(self):
        assert is_runaway_repetition(_ISSUE_UNIT * 500) is True
        assert len(_ISSUE_UNIT * 500) >= STOP_PATH_MIN_CHARS


class TestChatCompletionsLiveGuard:
    def test_content_loop_aborts_without_callback(self):
        agent = _streaming_agent()
        agent.stream_delta_callback = None
        agent._stream_callback = None
        tracking = _TrackingStream(_ISSUE_UNIT, n_chunks=5000, channel="content")

        response = _run_stream(agent, tracking)

        assert tracking.closed is True
        assert tracking.yielded < tracking.n_chunks
        assert response.choices[0].finish_reason == "stop"
        content = response.choices[0].message.content or ""
        assert len(content) >= STOP_PATH_MIN_CHARS
        assert len(content) < 32_000 + len(_ISSUE_UNIT)
        assert is_runaway_repetition(content) is True

    def test_content_loop_aborts_with_callback(self):
        agent = _streaming_agent()
        seen = []
        agent.stream_delta_callback = seen.append
        tracking = _TrackingStream(_ISSUE_UNIT, n_chunks=5000, channel="content")

        response = _run_stream(agent, tracking)

        assert tracking.closed is True
        assert tracking.yielded < tracking.n_chunks
        assert response.choices[0].finish_reason == "stop"
        assert is_runaway_repetition(response.choices[0].message.content or "") is True
        assert seen

    def test_reasoning_loop_aborts(self):
        agent = _streaming_agent()
        tracking = _TrackingStream(_ISSUE_UNIT, n_chunks=5000, channel="reasoning")

        response = _run_stream(agent, tracking)

        assert tracking.closed is True
        assert tracking.yielded < tracking.n_chunks
        assert response.choices[0].finish_reason == "stop"
        reasoning = getattr(response.choices[0].message, "reasoning_content", None)
        assert isinstance(reasoning, str) and len(reasoning) >= STOP_PATH_MIN_CHARS
        assert is_runaway_repetition(reasoning) is True

    def test_legit_batch_output_streams_fully(self):
        agent = _streaming_agent()
        rows = [
            f"INSERT INTO metrics (id, value) VALUES ({i}, {i * 7});\n" for i in range(600)
        ]
        assert len("".join(rows)) >= STOP_PATH_MIN_CHARS
        assert is_runaway_repetition("".join(rows)) is False

        chunks = [_make_stream_chunk(content=row) for row in rows]
        chunks.append(_make_stream_chunk(finish_reason="stop"))
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = iter(chunks)
        with patch.object(
            agent, "_create_request_openai_client", return_value=mock_client
        ):
            response = agent._interruptible_streaming_api_call({})

        assert response.choices[0].finish_reason == "stop"
        assert response.choices[0].message.content == "".join(rows)


class TestAnthropicLiveGuard:
    def _anthropic_agent(self):
        with (
            patch("model_tools.get_tool_definitions", return_value=[]),
            patch("model_tools.check_toolset_requirements", return_value={}),
        ):
            agent = AIAgent(
                api_key="test-key-1234567890",
                base_url="https://api.anthropic.com",
                quiet_mode=True,
                skip_context_files=True,
                skip_memory=True,
            )
            agent.api_mode = "anthropic_messages"
            agent._interrupt_requested = False
            return agent

    def _loop_manager(self, unit, n_events, *, channel="text"):
        events = []
        for _ in range(n_events):
            if channel == "text":
                delta = SimpleNamespace(type="text_delta", text=unit)
            else:
                delta = SimpleNamespace(type="thinking_delta", thinking=unit)
            events.append(
                SimpleNamespace(type="content_block_delta", delta=delta)
            )

        state = {"closed": False, "yielded": 0}

        class _RawStream:
            response = None

            def __iter__(self):
                return self

            def __next__(self):
                if state["closed"]:
                    raise StopIteration
                if state["yielded"] >= len(events):
                    raise StopIteration
                state["yielded"] += 1
                return events[state["yielded"] - 1]

            def close(self):
                state["closed"] = True

        raw = _RawStream()
        manager = MagicMock()
        manager.__enter__ = MagicMock(return_value=raw)
        manager.__exit__ = MagicMock(return_value=False)
        return manager, state

    def test_anthropic_text_loop_aborts(self):
        agent = self._anthropic_agent()
        agent._anthropic_client = MagicMock()
        manager, state = self._loop_manager(_ISSUE_UNIT, n_events=5000, channel="text")
        agent._anthropic_client.messages.stream.return_value = manager
        agent._create_request_anthropic_client = lambda *a, **k: agent._anthropic_client

        response = agent._interruptible_streaming_api_call({})

        assert state["yielded"] < 5000
        assert getattr(response, "stop_reason", None) == "end_turn"
        texts = [
            getattr(b, "text", "")
            for b in (getattr(response, "content", None) or [])
            if getattr(b, "type", None) == "text"
        ]
        assert is_runaway_repetition("".join(texts)) is True

    def test_anthropic_thinking_loop_aborts(self):
        agent = self._anthropic_agent()
        agent._anthropic_client = MagicMock()
        manager, state = self._loop_manager(
            _ISSUE_UNIT, n_events=5000, channel="thinking"
        )
        agent._anthropic_client.messages.stream.return_value = manager
        agent._create_request_anthropic_client = lambda *a, **k: agent._anthropic_client

        response = agent._interruptible_streaming_api_call({})

        assert state["yielded"] < 5000
        assert getattr(response, "stop_reason", None) == "end_turn"
        thoughts = [
            getattr(b, "thinking", "")
            for b in (getattr(response, "content", None) or [])
            if getattr(b, "type", None) == "thinking"
        ]
        assert is_runaway_repetition("".join(thoughts)) is True


class TestTurnEndsThroughRepetitionStop:
    def _loop_agent(self):
        return _streaming_agent()

    def test_full_turn_content_loop_ends_with_notice_not_persisted(self):
        agent = self._loop_agent()
        tracking = _TrackingStream(_ISSUE_UNIT, n_chunks=5000, channel="content")
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = tracking
        with (
            patch.object(
                agent, "_create_request_openai_client", return_value=mock_client
            ),
            patch.object(agent, "_persist_session"),
            patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
        ):
            result = agent.run_conversation("Summarize the backlog.")

        assert tracking.closed is True
        assert result["completed"] is False
        assert "Repetition" in (result["final_response"] or "")
        assert not any(
            isinstance(m, dict)
            and isinstance(m.get("content"), str)
            and _ISSUE_UNIT * 10 in m["content"]
            for m in result["messages"]
        )

    def test_stop_path_reasoning_loop_ends_with_notice(self):
        agent = self._loop_agent()
        loop_reasoning = _ISSUE_UNIT * 500
        assert len(loop_reasoning) >= STOP_PATH_MIN_CHARS
        message = SimpleNamespace(
            content="brief visible prefix",
            tool_calls=None,
            reasoning=loop_reasoning,
            reasoning_content=None,
            reasoning_details=None,
            refusal=None,
        )
        response = SimpleNamespace(
            id="completed-response",
            model="test/model",
            choices=[SimpleNamespace(index=0, message=message, finish_reason="stop")],
            usage=None,
        )
        agent.client = MagicMock()
        agent.client.chat.completions.create.side_effect = [response]
        with (
            patch.object(agent, "_persist_session"),
            patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
        ):
            result = agent.run_conversation("Summarize the backlog.")

        assert result["completed"] is False
        assert "Repetition" in (result["final_response"] or "")
