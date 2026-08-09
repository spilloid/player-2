"""Contract for player2.clock -- the single time source for the whole runtime.

Every subsystem (scheduler, capture, audio, recorder) timestamps from one clock so that
a recorded session can be replayed with frames, audio, and executed pad states aligned.
`SessionClock` is the real one; `ManualClock` exists so that timing-dependent code --
above all the scheduler -- is testable with no sleeping and no flakiness.
"""

import itertools
import math
import time

import pytest

from player2.clock import Clock, ManualClock, SessionClock
from player2.contracts import TICK_MS


class TestSessionClock:
    def test_starts_near_zero(self) -> None:
        c = SessionClock()
        assert 0.0 <= c.now_ms() < 50.0

    def test_is_monotonic_non_decreasing(self) -> None:
        c = SessionClock()
        samples = [c.now_ms() for _ in range(2000)]
        assert all(b >= a for a, b in itertools.pairwise(samples))

    def test_advances_over_real_time(self) -> None:
        c = SessionClock()
        t0 = c.now_ms()
        time.sleep(0.02)
        assert c.now_ms() - t0 >= 15.0

    def test_epoch_wall_correlates_session_time_to_wall_clock(self) -> None:
        """Recordings need to be locatable in real-world time, but the clock itself must
        never *derive* elapsed time from the wall clock (it can jump, or go backwards)."""
        before = time.time()
        c = SessionClock()
        after = time.time()
        assert before <= c.epoch_wall <= after

    def test_satisfies_clock_protocol(self) -> None:
        assert isinstance(SessionClock(), Clock)


class TestManualClock:
    def test_starts_at_zero_by_default(self) -> None:
        assert ManualClock().now_ms() == 0.0

    def test_starts_at_given_time(self) -> None:
        assert ManualClock(start_ms=1234.5).now_ms() == 1234.5

    def test_advance_moves_forward(self) -> None:
        c = ManualClock()
        c.advance(10.0)
        c.advance(5.5)
        assert c.now_ms() == pytest.approx(15.5)

    def test_advance_rejects_negative(self) -> None:
        """A clock that can go backwards silently corrupts every deadline computed from it."""
        c = ManualClock()
        with pytest.raises(ValueError):
            c.advance(-1.0)

    def test_advance_zero_is_allowed(self) -> None:
        c = ManualClock(start_ms=5.0)
        c.advance(0.0)
        assert c.now_ms() == 5.0

    def test_does_not_advance_on_its_own(self) -> None:
        c = ManualClock()
        first = c.now_ms()
        time.sleep(0.01)
        assert c.now_ms() == first

    def test_satisfies_clock_protocol(self) -> None:
        assert isinstance(ManualClock(), Clock)

    @pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
    def test_advance_rejects_non_finite(self, bad: float) -> None:
        """Review finding: NaN is not less than zero, so it slips past the negative check
        and then poisons now_ms() permanently -- every deadline computed from the clock
        becomes NaN, and every comparison against it silently returns False."""
        with pytest.raises(ValueError):
            ManualClock().advance(bad)

    @pytest.mark.parametrize("bad", ["10", None, object()])
    def test_advance_rejects_non_numeric(self, bad: object) -> None:
        with pytest.raises(ValueError):
            ManualClock().advance(bad)  # type: ignore[arg-type]

    @pytest.mark.parametrize("bad", [math.nan, math.inf])
    def test_rejects_non_finite_start(self, bad: float) -> None:
        with pytest.raises(ValueError):
            ManualClock(start_ms=bad)

    def test_stays_finite_after_many_advances(self) -> None:
        c = ManualClock()
        for _ in range(100):
            c.advance(TICK_MS)
        assert math.isfinite(c.now_ms())
