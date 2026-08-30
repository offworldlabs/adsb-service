"""Backoff engine tests: schedule, jitter bounds, stable-connection reset.

No sleeping — time is injected (DESIGN.md §7)."""

import random

from adsbhub_adapter.adapter import Backoff


class FakeClock:
    def __init__(self, start: float = 0.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make(initial=1.0, maximum=60.0, stable_reset=60.0, rng=lambda: 1.0, clock=None):
    return Backoff(initial, maximum, stable_reset, rng=rng, clock=clock or FakeClock())


class TestSchedule:
    def test_doubles_from_initial_to_cap(self):
        b = make()
        delays = [b.next_delay() for _ in range(10)]
        assert delays == [1, 2, 4, 8, 16, 32, 60, 60, 60, 60]

    def test_configurable_initial_and_cap(self):
        b = make(initial=0.25, maximum=10.0)
        delays = [b.next_delay() for _ in range(8)]
        assert delays == [0.25, 0.5, 1.0, 2.0, 4.0, 8.0, 10.0, 10.0]

    def test_stays_capped_forever(self):
        b = make()
        for _ in range(200):
            last = b.next_delay()
        assert last == 60.0


class TestJitter:
    def test_full_jitter_bounds(self):
        # spec §24: full jitter -> uniform(0, base); delay never exceeds the
        # un-jittered base for the attempt and never exceeds the cap.
        rng = random.Random(42)
        b = make(rng=rng.random)
        for expected_base in [1, 2, 4, 8, 16, 32, 60, 60, 60, 60]:
            delay = b.next_delay()
            assert 0.0 <= delay <= expected_base

    def test_jitter_actually_spreads(self):
        rng = random.Random(7)
        delays = set()
        for _ in range(20):
            b = make(rng=rng.random)
            delays.add(b.next_delay())
        assert len(delays) > 10


class TestReset:
    def test_reset_after_stable_connection(self):
        clock = FakeClock()
        b = make(clock=clock)
        assert [b.next_delay() for _ in range(3)] == [1, 2, 4]
        b.note_connected()
        clock.advance(61.0)
        b.note_disconnected()
        assert b.next_delay() == 1.0  # §24: 60 s stable resets the backoff

    def test_reset_at_exact_boundary(self):
        clock = FakeClock()
        b = make(clock=clock)
        b.next_delay()
        b.note_connected()
        clock.advance(60.0)
        b.note_disconnected()
        assert b.next_delay() == 1.0

    def test_no_reset_on_short_connection(self):
        clock = FakeClock()
        b = make(clock=clock)
        assert [b.next_delay() for _ in range(3)] == [1, 2, 4]
        b.note_connected()
        clock.advance(10.0)
        b.note_disconnected()
        assert b.next_delay() == 8.0  # progression continues

    def test_disconnect_without_connect_never_resets(self):
        b = make()
        assert [b.next_delay() for _ in range(2)] == [1, 2]
        b.note_disconnected()
        assert b.next_delay() == 4.0

    def test_repeated_flapping_keeps_growing(self):
        clock = FakeClock()
        b = make(clock=clock)
        delays = []
        for _ in range(7):
            b.note_connected()
            clock.advance(1.0)
            b.note_disconnected()
            delays.append(b.next_delay())
        assert delays == [1, 2, 4, 8, 16, 32, 60]

    def test_explicit_reset(self):
        b = make()
        for _ in range(5):
            b.next_delay()
        b.reset()
        assert b.next_delay() == 1.0
