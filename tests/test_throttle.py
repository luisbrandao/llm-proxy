"""The once-per-interval gate behind every "warn, but not on every request" line."""
import time

from app.throttle import Throttle


def test_it_opens_once_per_interval_and_per_key(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    gate = Throttle(60)

    assert gate.ready() is True, "the first time is always ready"
    assert gate.ready() is False
    assert gate.ready("other") is True, "keys do not share an interval"

    clock[0] += 59
    assert gate.ready() is False
    clock[0] += 1
    assert gate.ready() is True
    assert gate.ready() is False, "and the interval starts again"

    gate.reset()
    assert gate.ready() is True


def test_a_fresh_gate_is_ready_however_young_the_clock(monkeypatch):
    """Monotonic time counts from boot: a gate that compared against zero would
    stay shut for the first interval after the host started."""
    monkeypatch.setattr(time, "monotonic", lambda: 3.0)
    assert Throttle(300).ready() is True
