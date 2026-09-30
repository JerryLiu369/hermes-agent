"""Unit tests for repetition-dominated model output detection."""

from __future__ import annotations

import random

import pytest

from agent.repetition_guard import (
    MIN_FRAGMENT_LENGTH,
    STOP_PATH_MIN_CHARS,
    STREAM_REPETITION_MIN_CHARS,
    STREAM_REPETITION_TAIL_CHARS,
    StreamingRepetitionMonitor,
    is_repetition_dominated,
    is_runaway_repetition,
    live_repetition_tail,
)

# The exact sentence from the #86581 incident (echoed hundreds of times by
# the model before the provider cut it off at finish_reason=length).
_INCIDENT_ECHO = "好，你幫我更改成 Google Gemini 4 31B。"


class TestRepetitionGuard:
    def test_incident_shape_flags_repetition(self):
        # Narration + the echoed sentence on its own line, repeated (line path).
        text = ("We need to verify the model setting.\n" + _INCIDENT_ECHO + "\n") * 800
        assert is_repetition_dominated(text) is True

    def test_repeated_sentence_without_line_breaks_flags(self):
        # Repetition loop with no line breaks — exercises the window path.
        text = _INCIDENT_ECHO * 2000
        assert len(text) >= MIN_FRAGMENT_LENGTH
        assert is_repetition_dominated(text) is True

    def test_multiline_paragraph_run_uses_true_period_coverage(self):
        rng = random.Random(11)
        paragraph = "\n".join(
            "".join(rng.choice("abcdefghijklmnopqrstuvwxyz ") for _ in range(151))
            for _ in range(5)
        ) + "\n"

        # The incident unit was approximately 764 characters. Detection must
        # remain scale-free as the number of exact repeats grows.
        for repeat_count in (100, 1_000, 10_000):
            assert is_repetition_dominated(paragraph * repeat_count) is True

    @pytest.mark.parametrize("shape", ["unique_prefix_suffix", "counter_loop"])
    def test_dominant_run_with_unique_prefix_and_suffix_flags(self, shape):
        if shape == "counter_loop":
            # A changing counter breaks exact periodicity; main's window scan (#86581) must
            # still flag it.
            text = "".join(
                f"Step {i}: I will now carefully re-check the configuration file for the error again.\n"
                for i in range(200)
            )
            assert is_repetition_dominated(text) is True
            return
        paragraph = (
            "A deliberately long repeated paragraph has enough distinct text "
            "to make its period exceed the guard's minimum anchor length.\n"
            "It also spans multiple lines, matching the real incident shape.\n"
        )
        repeated = paragraph * 20
        text = ("unique introduction " * 20) + repeated + (" unique ending" * 20)

        assert len(repeated) > len(text) * 0.5
        assert is_repetition_dominated(text) is True

    def test_long_legitimate_text_not_flagged(self):
        # Long, unique prose — no 60-char window ever repeats.
        text = " ".join(
            f"Sentence number {i} describes a distinct topic with unique words "
            f"such as quasar-{i} and nebula-{i} to keep every window distinct."
            for i in range(1200)
        )
        assert len(text) >= MIN_FRAGMENT_LENGTH
        assert is_repetition_dominated(text) is False

    def test_short_fragment_never_flagged(self):
        # Below MIN_FRAGMENT_LENGTH the guard fails open — short truncations
        # are legitimately continued even if they look repetitive.
        assert is_repetition_dominated("A. " * 50) is False
        assert is_repetition_dominated("hello ") is False

    def test_repeat_not_dominant_not_flagged(self):
        # A repeated sentence scattered through a long unique text: repeated
        # windows exist but cover far less than half of the fragment.
        filler = " ".join(f"unique filler token {i}" for i in range(3000))
        text = filler + ("\n" + _INCIDENT_ECHO + "\n") * 30
        assert is_repetition_dominated(text) is False

    def test_non_string_inputs(self):
        assert is_repetition_dominated("") is False
        assert is_repetition_dominated(None) is False
        assert is_repetition_dominated(12345) is False


class TestStreamingRepetitionGuard:
    def test_monitor_initial_state(self):
        monitor = StreamingRepetitionMonitor()
        assert monitor.content_len == 0
        assert monitor.reasoning_len == 0
        assert monitor.next_content_check == STREAM_REPETITION_MIN_CHARS
        assert monitor.next_reasoning_check == STREAM_REPETITION_MIN_CHARS
        assert monitor.next_content_check == 16_000

    def test_monitor_content_doubling(self):
        monitor = StreamingRepetitionMonitor()
        # Adding less than threshold -> False
        assert monitor.add_content(10_000) is False
        assert monitor.content_len == 10_000
        # Reaching threshold (16_000) -> True
        assert monitor.add_content(6_000) is True
        assert monitor.content_len == 16_000

        # Advance threshold doubles next check to 32_000
        monitor.advance_content()
        assert monitor.next_content_check == 32_000
        assert monitor.add_content(15_000) is False
        assert monitor.add_content(1_000) is True
        assert monitor.content_len == 32_000

        # Next doubling is 64_000
        monitor.advance_content()
        assert monitor.next_content_check == 64_000

    def test_monitor_reasoning_independent(self):
        monitor = StreamingRepetitionMonitor()
        # Reasoning progresses independently of content
        assert monitor.add_reasoning(16_000) is True
        assert monitor.reasoning_len == 16_000
        assert monitor.content_len == 0
        assert monitor.next_content_check == 16_000

        monitor.advance_reasoning()
        assert monitor.next_reasoning_check == 32_000
        assert monitor.next_content_check == 16_000

        # Content can still trigger at 16_000
        assert monitor.add_content(16_000) is True
        assert monitor.content_len == 16_000

    def test_live_repetition_tail_bounding(self):
        # Short text returned as-is
        short = "a" * 100
        assert live_repetition_tail(short) == short

        # Text equal to tail chars returned as-is
        exact = "x" * STREAM_REPETITION_TAIL_CHARS
        assert live_repetition_tail(exact) == exact

        # Text longer than tail chars truncated to last STREAM_REPETITION_TAIL_CHARS
        long_text = ("prefix_" * 10_000) + ("tail_" * 15_000)
        tail = live_repetition_tail(long_text)
        assert len(tail) == STREAM_REPETITION_TAIL_CHARS
        assert tail == long_text[-STREAM_REPETITION_TAIL_CHARS:]

    def test_streaming_runaway_content_and_reasoning_detection(self):
        repeated_sentence = "The model is currently processing your request and analyzing the results.\n"
        loop_text = repeated_sentence * 300
        assert len(loop_text) >= STOP_PATH_MIN_CHARS

        # live_repetition_tail feeding into is_runaway_repetition
        tail = live_repetition_tail(loop_text)
        assert is_runaway_repetition(tail) is True

        # Non-repetitive text of equal length must not trigger
        unique_text = "\n".join(f"Unique line {i} explaining distinct topic details." for i in range(500))
        assert len(unique_text) >= STOP_PATH_MIN_CHARS
        assert is_runaway_repetition(live_repetition_tail(unique_text)) is False
