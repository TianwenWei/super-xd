"""Acoustic framing and endpoint decisions use model probabilities, never text."""
import pytest

from super_xd.demo.audio.vad import SpeechGate


def feed(gate, probability, count):
    return [gate.feed(bytes(1024), probability) for _ in range(count)]


def test_speech_start_keeps_preroll_and_requires_sustained_voice():
    gate = SpeechGate()
    assert all(item['event'] is None for item in feed(gate, .01, 20))
    assert feed(gate, .9, 1)[0]['event'] is None
    assert feed(gate, .01, 1)[0]['event'] is None
    result = feed(gate, .9, 3)[-1]
    assert result['event'] == 'start'
    assert len(result['audio']) == 10  # 320 ms includes the confirming frames.


def test_pause_hysteresis_and_next_utterance():
    gate = SpeechGate()
    feed(gate, .9, 3)
    assert all(item['event'] is None for item in feed(gate, .1, 19))
    assert feed(gate, .5, 1)[0]['event'] is None
    assert all(item['event'] is None for item in feed(gate, .1, 19))
    assert feed(gate, .1, 1)[0]['event'] == 'end'
    assert feed(gate, .9, 3)[-1]['event'] == 'start'


def test_gate_refuses_invalid_probabilities_and_incomplete_frames():
    gate = SpeechGate()
    for probability in [float('nan'), float('inf'), -1, 2]:
        with pytest.raises(ValueError):
            gate.feed(bytes(1024), probability)
    with pytest.raises(ValueError):
        gate.feed(bytes(32), .9)
