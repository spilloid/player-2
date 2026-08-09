"""Contract for player2.control -- the motor system.

This is the most safety-critical code in the project. It is the only thing that writes to
the controller, and if it stops running while a stick is held, the character keeps walking.

The design that makes it testable: `Scheduler` is a tick-driven state machine with no
threads and no wall clock. Tests drive it with a `ManualClock` and call `tick()` by hand,
so preemption, deadman expiry, and takeover races are deterministic rather than flaky.
`SchedulerThread` is a thin absolute-deadline loop around it, tested separately.

Invariants under test:
  * The scheduler is the SOLE writer to the output. Everything else submits commands.
  * A new chunk resolves against NEUTRAL. Anything it does not mention is released.
  * If no fresh chunk arrives, the deadman releases everything -- silence means stop.
  * A human takeover wins immediately, and cannot be undone by a decision that was
    already in flight when the human grabbed the controller.
"""

import threading
import time

import pytest

from player2.clock import ManualClock, SessionClock
from player2.contracts import (
    NEUTRAL,
    TICK_MS,
    ActionChunk,
    Button,
    Keyframe,
    PadState,
)
from player2.control.base import IControllerOutput
from player2.control.null import NullControllerAdapter
from player2.control.scheduler import EventKind, Scheduler, SchedulerThread

MAX_HOLD_MS = 250.0


def chunk(*keyframes: Keyframe, seq: int = 0, epoch: int = 0) -> ActionChunk:
    return ActionChunk(keyframes=tuple(keyframes), decision_seq=seq, epoch=epoch)


def walk_right(duration_ms: float = 100.0, seq: int = 0, epoch: int = 0) -> ActionChunk:
    return chunk(
        Keyframe(t_ms=0.0, left_stick=(1.0, 0.0)),
        Keyframe(t_ms=duration_ms, left_stick=(1.0, 0.0)),
        seq=seq,
        epoch=epoch,
    )


@pytest.fixture
def rig() -> tuple[Scheduler, NullControllerAdapter, ManualClock]:
    clock = ManualClock()
    out = NullControllerAdapter()
    sched = Scheduler(output=out, clock=clock, max_hold_ms=MAX_HOLD_MS)
    return sched, out, clock


def run_until(sched: Scheduler, clock: ManualClock, t_ms: float) -> None:
    """Tick the scheduler forward to t_ms at the nominal tick rate."""
    while clock.now_ms() < t_ms:
        clock.advance(TICK_MS)
        sched.tick()


class TestOutputAdapterContract:
    def test_null_adapter_satisfies_the_interface(self) -> None:
        assert isinstance(NullControllerAdapter(), IControllerOutput)

    def test_null_adapter_records_every_state_it_is_given(self) -> None:
        out = NullControllerAdapter()
        out.set_state(NEUTRAL)
        out.set_state(PadState(left_trigger=1.0))
        assert [s.left_trigger for s in out.states] == [0.0, 1.0]

    def test_reset_records_a_neutral_state(self) -> None:
        out = NullControllerAdapter()
        out.set_state(PadState(left_trigger=1.0))
        out.reset()
        assert out.states[-1] == NEUTRAL


class TestIdleBehaviour:
    def test_holds_neutral_with_no_chunk(self, rig: tuple[Scheduler, NullControllerAdapter,
                                                          ManualClock]) -> None:
        sched, out, clock = rig
        run_until(sched, clock, 100.0)
        assert all(s == NEUTRAL for s in out.states)

    def test_writes_on_every_tick(self, rig: tuple[Scheduler, NullControllerAdapter,
                                                   ManualClock]) -> None:
        """One write per tick keeps 'what the pad actually did' trivially recordable, and
        120Hz of USB reports is nothing. Deduplicating would save nothing and would make
        the recorded stream lie by omission."""
        sched, out, clock = rig
        for _ in range(10):
            clock.advance(TICK_MS)
            sched.tick()
        assert len(out.states) == 10


class TestChunkExecution:
    def test_executes_a_submitted_chunk(self, rig: tuple[Scheduler, NullControllerAdapter,
                                                         ManualClock]) -> None:
        sched, out, clock = rig
        assert sched.submit(walk_right(100.0)) is True
        run_until(sched, clock, 50.0)
        assert out.states[-1].left_stick == pytest.approx((1.0, 0.0))

    def test_interpolates_across_the_chunk(self, rig: tuple[Scheduler, NullControllerAdapter,
                                                            ManualClock]) -> None:
        sched, out, clock = rig
        sched.submit(chunk(
            Keyframe(t_ms=0.0, left_stick=(0.0, 0.0)),
            Keyframe(t_ms=100.0, left_stick=(1.0, 0.0)),
        ))
        run_until(sched, clock, 50.0)
        assert out.states[-1].left_stick[0] == pytest.approx(0.5, abs=0.02)

    def test_timeline_is_anchored_at_acceptance_not_at_authoring(
        self, rig: tuple[Scheduler, NullControllerAdapter, ManualClock]
    ) -> None:
        """t_ms is relative to the moment the scheduler ACCEPTS the chunk. Anchoring it
        anywhere else (proposal time, enqueue time) makes the offset vary with model
        latency, which would smear every recorded action against its frames."""
        sched, out, clock = rig
        run_until(sched, clock, 500.0)
        sched.submit(walk_right(100.0))
        clock.advance(TICK_MS)
        sched.tick()
        assert out.states[-1].left_stick == pytest.approx((1.0, 0.0))

    def test_holds_final_state_until_the_deadman(self, rig: tuple[Scheduler,
                                                                  NullControllerAdapter,
                                                                  ManualClock]) -> None:
        sched, out, clock = rig
        sched.submit(walk_right(100.0))
        run_until(sched, clock, 300.0)
        assert out.states[-1].left_stick == pytest.approx((1.0, 0.0))


class TestDeadman:
    def test_releases_everything_after_max_hold(self, rig: tuple[Scheduler,
                                                                 NullControllerAdapter,
                                                                 ManualClock]) -> None:
        """Silence must mean stop. If the model dies mid-session, the character must not
        keep sprinting into the void."""
        sched, out, clock = rig
        sched.submit(walk_right(100.0))
        run_until(sched, clock, 100.0 + MAX_HOLD_MS + TICK_MS * 2)
        assert out.states[-1] == NEUTRAL

    def test_does_not_release_early(self, rig: tuple[Scheduler, NullControllerAdapter,
                                                     ManualClock]) -> None:
        sched, out, clock = rig
        sched.submit(walk_right(100.0))
        run_until(sched, clock, 100.0 + MAX_HOLD_MS - TICK_MS * 2)
        assert out.states[-1].left_stick == pytest.approx((1.0, 0.0))

    def test_deadline_is_measured_from_acceptance_plus_duration(
        self, rig: tuple[Scheduler, NullControllerAdapter, ManualClock]
    ) -> None:
        sched, out, clock = rig
        run_until(sched, clock, 1000.0)
        sched.submit(walk_right(100.0))
        run_until(sched, clock, 1000.0 + 100.0 + MAX_HOLD_MS - TICK_MS * 2)
        assert out.states[-1].left_stick == pytest.approx((1.0, 0.0))
        run_until(sched, clock, 1000.0 + 100.0 + MAX_HOLD_MS + TICK_MS * 2)
        assert out.states[-1] == NEUTRAL

    def test_a_fresh_chunk_rearms_it(self, rig: tuple[Scheduler, NullControllerAdapter,
                                                      ManualClock]) -> None:
        sched, out, clock = rig
        sched.submit(walk_right(100.0, seq=1))
        run_until(sched, clock, 200.0)
        sched.submit(walk_right(100.0, seq=2))
        run_until(sched, clock, 200.0 + 100.0 + MAX_HOLD_MS - TICK_MS * 2)
        assert out.states[-1].left_stick == pytest.approx((1.0, 0.0))

    def test_emits_an_event_once(self, rig: tuple[Scheduler, NullControllerAdapter,
                                                  ManualClock]) -> None:
        sched, _out, clock = rig
        sched.submit(walk_right(100.0))
        run_until(sched, clock, 1000.0)
        kinds = [e.kind for e in sched.drain_events()]
        assert kinds.count(EventKind.DEADMAN) == 1

    def test_stays_neutral_after_expiry(self, rig: tuple[Scheduler, NullControllerAdapter,
                                                         ManualClock]) -> None:
        sched, out, clock = rig
        sched.submit(walk_right(100.0))
        run_until(sched, clock, 2000.0)
        assert all(s == NEUTRAL for s in out.states[-20:])


class TestPreemption:
    def test_a_new_chunk_replaces_the_running_one(self, rig: tuple[Scheduler,
                                                                   NullControllerAdapter,
                                                                   ManualClock]) -> None:
        sched, out, clock = rig
        sched.submit(walk_right(1000.0, seq=1))
        run_until(sched, clock, 100.0)
        sched.submit(chunk(Keyframe(t_ms=0.0, left_stick=(-1.0, 0.0)), seq=2))
        clock.advance(TICK_MS)
        sched.tick()
        assert out.states[-1].left_stick == pytest.approx((-1.0, 0.0))

    def test_unmentioned_analog_is_released_not_inherited(
        self, rig: tuple[Scheduler, NullControllerAdapter, ManualClock]
    ) -> None:
        """THE safety test. The old chunk holds the stick hard right. The new chunk only
        presses a button. If the stick survived, the character would keep walking with no
        instruction ever having said so -- the exact bug that motivates resolving against
        NEUTRAL rather than against live state."""
        sched, out, clock = rig
        sched.submit(walk_right(1000.0, seq=1))
        run_until(sched, clock, 100.0)
        assert out.states[-1].left_stick == pytest.approx((1.0, 0.0))

        sched.submit(chunk(
            Keyframe(t_ms=0.0, buttons=frozenset({Button.A})),
            Keyframe(t_ms=100.0, buttons=frozenset({Button.A})),
            seq=2,
        ))
        clock.advance(TICK_MS)
        sched.tick()
        assert out.states[-1].left_stick == (0.0, 0.0)
        assert out.states[-1].buttons == frozenset({Button.A})

    def test_buttons_held_by_the_old_chunk_are_released(
        self, rig: tuple[Scheduler, NullControllerAdapter, ManualClock]
    ) -> None:
        sched, out, clock = rig
        sched.submit(chunk(
            Keyframe(t_ms=0.0, buttons=frozenset({Button.X})),
            Keyframe(t_ms=1000.0, buttons=frozenset({Button.X})),
            seq=1,
        ))
        run_until(sched, clock, 100.0)
        sched.submit(chunk(Keyframe(t_ms=0.0, buttons=frozenset({Button.B})), seq=2))
        clock.advance(TICK_MS)
        sched.tick()
        assert out.states[-1].buttons == frozenset({Button.B})

    def test_emits_a_preemption_event(self, rig: tuple[Scheduler, NullControllerAdapter,
                                                       ManualClock]) -> None:
        sched, _out, clock = rig
        sched.submit(walk_right(1000.0, seq=1))
        run_until(sched, clock, 50.0)
        sched.drain_events()
        sched.submit(walk_right(100.0, seq=2))
        clock.advance(TICK_MS)
        sched.tick()
        assert EventKind.PREEMPTED in [e.kind for e in sched.drain_events()]


class TestStaleDecisionRejection:
    def test_rejects_an_older_decision_seq(self, rig: tuple[Scheduler, NullControllerAdapter,
                                                            ManualClock]) -> None:
        """Arrival order is not intent order. A slow proposal that finishes after a newer
        one must not overwrite it."""
        sched, out, clock = rig
        assert sched.submit(walk_right(1000.0, seq=5)) is True
        run_until(sched, clock, 50.0)
        assert sched.submit(chunk(Keyframe(t_ms=0.0, left_stick=(-1.0, 0.0)), seq=3)) is False
        clock.advance(TICK_MS)
        sched.tick()
        assert out.states[-1].left_stick == pytest.approx((1.0, 0.0))

    def test_rejects_an_equal_decision_seq(self, rig: tuple[Scheduler, NullControllerAdapter,
                                                            ManualClock]) -> None:
        sched, _out, _clock = rig
        assert sched.submit(walk_right(100.0, seq=5)) is True
        assert sched.submit(walk_right(100.0, seq=5)) is False

    def test_accepts_the_first_chunk_at_seq_zero(self, rig: tuple[Scheduler,
                                                                  NullControllerAdapter,
                                                                  ManualClock]) -> None:
        sched, _out, _clock = rig
        assert sched.submit(walk_right(100.0, seq=0)) is True

    def test_a_rejected_chunk_does_not_rearm_the_deadman(
        self, rig: tuple[Scheduler, NullControllerAdapter, ManualClock]
    ) -> None:
        sched, out, clock = rig
        sched.submit(walk_right(100.0, seq=5))
        run_until(sched, clock, 200.0)
        sched.submit(walk_right(100.0, seq=1))
        run_until(sched, clock, 100.0 + MAX_HOLD_MS + TICK_MS * 2)
        assert out.states[-1] == NEUTRAL

    def test_emits_a_rejection_event_with_a_reason(
        self, rig: tuple[Scheduler, NullControllerAdapter, ManualClock]
    ) -> None:
        sched, _out, _clock = rig
        sched.submit(walk_right(100.0, seq=5))
        sched.drain_events()
        sched.submit(walk_right(100.0, seq=1))
        events = [e for e in sched.drain_events() if e.kind == EventKind.REJECTED]
        assert events and events[0].detail


class TestInvalidChunks:
    def test_rejects_an_invalid_chunk(self, rig: tuple[Scheduler, NullControllerAdapter,
                                                       ManualClock]) -> None:
        sched, _out, _clock = rig
        assert sched.submit(ActionChunk(keyframes=())) is False

    def test_an_invalid_chunk_does_not_disturb_the_running_one(
        self, rig: tuple[Scheduler, NullControllerAdapter, ManualClock]
    ) -> None:
        sched, out, clock = rig
        sched.submit(walk_right(1000.0, seq=1))
        run_until(sched, clock, 100.0)
        sched.submit(ActionChunk(keyframes=(), decision_seq=2))
        clock.advance(TICK_MS)
        sched.tick()
        assert out.states[-1].left_stick == pytest.approx((1.0, 0.0))

    def test_an_invalid_chunk_does_not_rearm_the_deadman(
        self, rig: tuple[Scheduler, NullControllerAdapter, ManualClock]
    ) -> None:
        sched, out, clock = rig
        sched.submit(walk_right(100.0, seq=1))
        run_until(sched, clock, 200.0)
        sched.submit(ActionChunk(keyframes=(), decision_seq=2))
        run_until(sched, clock, 100.0 + MAX_HOLD_MS + TICK_MS * 2)
        assert out.states[-1] == NEUTRAL

    def test_a_garbage_chunk_never_escapes_as_an_undeclared_exception(
        self, rig: tuple[Scheduler, NullControllerAdapter, ManualClock]
    ) -> None:
        """The scheduler thread is the only thing that can release the pad. It must not be
        killable by a malformed proposal."""
        sched, _out, _clock = rig
        for bad in [None, "chunk", 5, ActionChunk(keyframes=("x",))]:  # type: ignore[arg-type]
            assert sched.submit(bad) is False  # type: ignore[arg-type]


class TestHumanTakeover:
    def test_neutralises_immediately_without_waiting_for_a_tick(
        self, rig: tuple[Scheduler, NullControllerAdapter, ManualClock]
    ) -> None:
        """Deliberately does NOT tick before asserting. An earlier version of this test
        called tick() first, which meant it only proved "neutral eventually" -- and the
        implementation duly only mutated memory. If the loop is late, stopped, or dead
        when the human grabs the controller, "eventually" never arrives and the stick
        stays down in a live game."""
        sched, out, clock = rig
        sched.submit(walk_right(1000.0, seq=1))
        run_until(sched, clock, 100.0)
        assert out.states[-1].left_stick == pytest.approx((1.0, 0.0))
        sched.take_controller()
        assert out.states[-1] == NEUTRAL

    def test_bumps_the_epoch(self, rig: tuple[Scheduler, NullControllerAdapter,
                                              ManualClock]) -> None:
        sched, _out, _clock = rig
        before = sched.epoch
        sched.take_controller()
        assert sched.epoch > before

    def test_rejects_chunks_authored_before_the_takeover(
        self, rig: tuple[Scheduler, NullControllerAdapter, ManualClock]
    ) -> None:
        """A proposal already in flight when the human grabbed the controller must not be
        able to reactivate the pad when it finally lands."""
        sched, out, clock = rig
        stale_epoch = sched.epoch
        sched.take_controller()
        assert sched.submit(walk_right(100.0, seq=9, epoch=stale_epoch)) is False
        clock.advance(TICK_MS)
        sched.tick()
        assert out.states[-1] == NEUTRAL

    def test_stays_neutral_while_held(self, rig: tuple[Scheduler, NullControllerAdapter,
                                                       ManualClock]) -> None:
        sched, out, clock = rig
        sched.take_controller()
        run_until(sched, clock, 500.0)
        assert all(s == NEUTRAL for s in out.states)

    def test_return_controller_bumps_the_epoch_again(
        self, rig: tuple[Scheduler, NullControllerAdapter, ManualClock]
    ) -> None:
        """Decisions made *during* the takeover are also stale: the agent was watching a
        game being driven by someone else."""
        sched, _out, _clock = rig
        sched.take_controller()
        during = sched.epoch
        sched.return_controller()
        assert sched.epoch > during
        assert sched.submit(walk_right(100.0, epoch=during)) is False

    def test_accepts_chunks_again_after_return(
        self, rig: tuple[Scheduler, NullControllerAdapter, ManualClock]
    ) -> None:
        sched, out, clock = rig
        sched.take_controller()
        sched.return_controller()
        assert sched.submit(walk_right(100.0, seq=1, epoch=sched.epoch)) is True
        clock.advance(TICK_MS)
        sched.tick()
        assert out.states[-1].left_stick == pytest.approx((1.0, 0.0))

    def test_emits_takeover_and_return_events(
        self, rig: tuple[Scheduler, NullControllerAdapter, ManualClock]
    ) -> None:
        sched, _out, _clock = rig
        sched.take_controller()
        sched.return_controller()
        kinds = [e.kind for e in sched.drain_events()]
        assert EventKind.TAKEOVER in kinds and EventKind.RETURNED in kinds

    def test_is_idempotent(self, rig: tuple[Scheduler, NullControllerAdapter,
                                            ManualClock]) -> None:
        sched, out, clock = rig
        sched.take_controller()
        sched.take_controller()
        run_until(sched, clock, 100.0)
        assert all(s == NEUTRAL for s in out.states)


class TestShutdown:
    def test_stop_releases_the_pad(self, rig: tuple[Scheduler, NullControllerAdapter,
                                                    ManualClock]) -> None:
        """If we exit with a stick held, the character keeps moving in a live game."""
        sched, out, clock = rig
        sched.submit(walk_right(1000.0))
        run_until(sched, clock, 100.0)
        sched.close()
        assert out.states[-1] == NEUTRAL

    def test_close_is_idempotent(self, rig: tuple[Scheduler, NullControllerAdapter,
                                                  ManualClock]) -> None:
        sched, _out, _clock = rig
        sched.close()
        sched.close()

    def test_rejects_submissions_after_close(self, rig: tuple[Scheduler, NullControllerAdapter,
                                                              ManualClock]) -> None:
        sched, _out, _clock = rig
        sched.close()
        assert sched.submit(walk_right(100.0)) is False


class TestSchedulerThread:
    """The thread is deliberately thin: an absolute-deadline loop that calls tick()."""

    def test_runs_and_stops_cleanly(self) -> None:
        out = NullControllerAdapter()
        sched = Scheduler(output=out, clock=SessionClock(), max_hold_ms=MAX_HOLD_MS)
        thread = SchedulerThread(sched)
        thread.start()
        time.sleep(0.1)
        thread.stop()
        assert not thread.is_alive()
        assert out.states[-1] == NEUTRAL

    def test_ticks_at_roughly_the_nominal_rate(self) -> None:
        out = NullControllerAdapter()
        sched = Scheduler(output=out, clock=SessionClock(), max_hold_ms=MAX_HOLD_MS)
        thread = SchedulerThread(sched)
        thread.start()
        time.sleep(0.5)
        thread.stop()
        # 120Hz for 0.5s is ~60 ticks. Windows timer granularity is coarse, so this is a
        # sanity band, not a precision claim.
        assert 25 <= len(out.states) <= 120

    def test_does_not_burst_after_a_stall(self) -> None:
        """After a late tick the loop must SKIP missed ticks, not fire them back to back.
        Bursting would replay a stale timeline into a live game at high speed."""
        out = NullControllerAdapter()
        sched = Scheduler(output=out, clock=SessionClock(), max_hold_ms=MAX_HOLD_MS)
        thread = SchedulerThread(sched)
        thread.start()
        time.sleep(0.05)
        before = len(out.states)
        time.sleep(0.2)  # simulated observation window
        thread.stop()
        after = len(out.states)
        assert after - before <= 40

    def test_keeps_ticking_while_a_slow_decision_blocks_another_thread(self) -> None:
        """The claim 'controller execution is independent of model latency' is made
        falsifiable here: a 1s blocking propose() on another thread must not stall the pad."""
        out = NullControllerAdapter()
        sched = Scheduler(output=out, clock=SessionClock(), max_hold_ms=MAX_HOLD_MS)
        thread = SchedulerThread(sched)
        thread.start()

        def slow_model() -> None:
            time.sleep(1.0)
            sched.submit(walk_right(100.0, seq=1))

        worker = threading.Thread(target=slow_model, daemon=True)
        worker.start()
        time.sleep(0.3)
        mid = len(out.states)
        assert mid > 10, "scheduler stalled while another thread was blocked"
        worker.join(timeout=3.0)
        thread.stop()

    def test_records_tick_lateness(self) -> None:
        out = NullControllerAdapter()
        sched = Scheduler(output=out, clock=SessionClock(), max_hold_ms=MAX_HOLD_MS)
        thread = SchedulerThread(sched)
        thread.start()
        time.sleep(0.2)
        thread.stop()
        assert sched.max_lateness_ms >= 0.0

    def test_uses_the_schedulers_clock_not_its_own(self) -> None:
        """One clock. If the loop times itself from a private SessionClock while the
        scheduler evaluates deadlines from the injected one, cadence and deadman disagree
        the moment anyone injects a scaled, paused, or simulated clock."""
        sched = Scheduler(output=NullControllerAdapter(), clock=SessionClock(),
                          max_hold_ms=MAX_HOLD_MS)
        assert SchedulerThread(sched).clock is sched.clock

    def test_releases_the_pad_if_the_loop_raises(self) -> None:
        """Best-effort, and explicitly not a defence against the thread hanging -- nothing
        in-process can be. But an exception must not leave the sticks held."""
        class ExplodingOutput(NullControllerAdapter):
            calls = 0

            def set_state(self, state: PadState) -> None:
                ExplodingOutput.calls += 1
                if ExplodingOutput.calls == 3:
                    raise RuntimeError("device fell over")
                super().set_state(state)

        out = ExplodingOutput()
        sched = Scheduler(output=out, clock=SessionClock(), max_hold_ms=MAX_HOLD_MS)
        thread = SchedulerThread(sched)
        thread.start()
        time.sleep(0.2)
        thread.stop()
        assert out.states[-1] == NEUTRAL


# ---------------------------------------------------------------------------
# Hardening. Every test below came out of the Unit 2 adversarial review.
#
# They share one failure mode, which is the only one that really matters here:
# THE PAD ENDS UP STUCK HOLDING AN INPUT AND NOTHING CAN RELEASE IT.
# A stuck scheduler is not a hung process, it is a character sprinting into a
# lake while the model cheerfully proposes new plans that never get executed.
# ---------------------------------------------------------------------------


class SlowOutput(NullControllerAdapter):
    """An output whose first write blocks, standing in for a wedged device driver."""

    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def set_state(self, state: PadState) -> None:
        super().set_state(state)
        if not self.entered.is_set():
            self.entered.set()
            self.release.wait(5.0)


def call_with_timeout(fn: object, timeout: float = 2.0) -> bool:
    """Run fn on a thread; return True if it finished within the timeout."""
    done = threading.Event()

    def runner() -> None:
        try:
            fn()  # type: ignore[operator]
        finally:
            done.set()

    threading.Thread(target=runner, daemon=True).start()
    return done.wait(timeout)


class TestBlockedDeviceCannotWedgeTheEscapeHatches:
    """Controller I/O must not happen while holding the lock that takeover and close need.

    If it does, a driver that blocks for even a second takes every escape route with it:
    take_controller() blocks, close() blocks, and stop() joins the stuck thread before it
    ever tries to release. The one situation where a human most needs to grab the
    controller is exactly the situation where they cannot.
    """

    def test_take_controller_does_not_block_behind_a_stuck_write(self) -> None:
        out = SlowOutput()
        sched = Scheduler(output=out, clock=SessionClock(), max_hold_ms=MAX_HOLD_MS)
        sched.submit(walk_right(1000.0, seq=1))
        threading.Thread(target=sched.tick, daemon=True).start()
        assert out.entered.wait(2.0), "output was never called"
        try:
            assert call_with_timeout(sched.take_controller), "take_controller() blocked"
        finally:
            out.release.set()

    def test_close_does_not_block_behind_a_stuck_write(self) -> None:
        out = SlowOutput()
        sched = Scheduler(output=out, clock=SessionClock(), max_hold_ms=MAX_HOLD_MS)
        sched.submit(walk_right(1000.0, seq=1))
        threading.Thread(target=sched.tick, daemon=True).start()
        assert out.entered.wait(2.0), "output was never called"
        try:
            assert call_with_timeout(sched.close), "close() blocked"
        finally:
            out.release.set()

    def test_epoch_read_does_not_block_behind_a_stuck_write(self) -> None:
        out = SlowOutput()
        sched = Scheduler(output=out, clock=SessionClock(), max_hold_ms=MAX_HOLD_MS)
        sched.submit(walk_right(1000.0, seq=1))
        threading.Thread(target=sched.tick, daemon=True).start()
        assert out.entered.wait(2.0), "output was never called"
        try:
            assert call_with_timeout(lambda: sched.epoch), "epoch read blocked"
        finally:
            out.release.set()


class TestCloseIsRetryable:
    def test_a_failed_release_does_not_permanently_disable_close(self) -> None:
        """close() must not mark itself done before the pad is actually released. If the
        first neutral write throws and the flag is already set, every later close() returns
        instantly and the controller is never released at all."""
        class FlakyOutput(NullControllerAdapter):
            fail_next = True

            def set_state(self, state: PadState) -> None:
                if FlakyOutput.fail_next:
                    FlakyOutput.fail_next = False
                    raise RuntimeError("device busy")
                super().set_state(state)

        out = FlakyOutput()
        sched = Scheduler(output=out, clock=ManualClock(), max_hold_ms=MAX_HOLD_MS)
        sched.submit(walk_right(1000.0))
        try:
            sched.close()
        except RuntimeError:
            pass
        sched.close()
        assert out.states and out.states[-1] == NEUTRAL

    def test_falls_back_to_reset_when_set_state_keeps_failing(self) -> None:
        class OnlyResetWorks(NullControllerAdapter):
            def set_state(self, state: PadState) -> None:
                raise RuntimeError("device busy")

        out = OnlyResetWorks()
        sched = Scheduler(output=out, clock=ManualClock(), max_hold_ms=MAX_HOLD_MS)
        sched.close()
        assert out.reset_calls >= 1


class TestHostileClock:
    """Clock is a user-implementable Protocol, so its output is untrusted too."""

    def test_a_nan_clock_cannot_disable_the_deadman(self) -> None:
        """`now_ms > deadline_ms` is False forever once either side is NaN, so a held
        command would never expire. Fail safe: unusable time means release."""
        class NaNClock:
            def __init__(self) -> None:
                self.calls = 0

            def now_ms(self) -> float:
                self.calls += 1
                return 0.0 if self.calls < 3 else float("nan")

        out = NullControllerAdapter()
        sched = Scheduler(output=out, clock=NaNClock(), max_hold_ms=MAX_HOLD_MS)
        sched.submit(walk_right(1000.0))
        for _ in range(6):
            sched.tick()
        assert out.states[-1] == NEUTRAL

    def test_a_raising_clock_does_not_deadlock_submit(self) -> None:
        """Regression guard rather than a bug fix. Claude's review predicted a self-deadlock
        here -- submit()'s failure path re-acquires the non-reentrant lock -- and was WRONG:
        `with` releases the lock via __exit__ as the exception propagates, before the
        enclosing except clause runs. The test stays because the reasoning only holds for
        `with`; anyone who later switches to a manual acquire/release would deadlock the
        caller and every subsequent tick at once, which is the worst outcome available."""
        class RaisingClock:
            def now_ms(self) -> float:
                raise RuntimeError("clock died")

        sched = Scheduler(output=NullControllerAdapter(), clock=RaisingClock(),
                          max_hold_ms=MAX_HOLD_MS)
        result: list[bool] = []
        assert call_with_timeout(
            lambda: result.append(sched.submit(walk_right(100.0)))
        ), "submit deadlocked"
        assert result == [False]

    def test_a_raising_clock_leaves_the_pad_neutral_on_tick(self) -> None:
        class RaisingClock:
            def now_ms(self) -> float:
                raise RuntimeError("clock died")

        out = NullControllerAdapter()
        sched = Scheduler(output=out, clock=RaisingClock(), max_hold_ms=MAX_HOLD_MS)
        sched.tick()
        assert out.states[-1] == NEUTRAL


class TestQueuesAreBounded:
    def test_events_do_not_grow_without_bound_when_nobody_drains(self) -> None:
        """Nothing drains events until the recorder exists. An unbounded buffer inside the
        safety-critical process is a slow memory leak that ends with the OOM killer taking
        out the only thing that can release the controller."""
        sched = Scheduler(output=NullControllerAdapter(), clock=ManualClock(),
                          max_hold_ms=MAX_HOLD_MS)
        for i in range(20000):
            sched.submit(walk_right(100.0, seq=0 if i % 2 else -1))
        assert len(sched.drain_events()) <= 4096

    def test_pending_chunks_do_not_accumulate_between_ticks(self) -> None:
        """Only the newest accepted chunk can ever run, so retaining the rest just makes
        tick() do unbounded work while holding up the write."""
        clock = ManualClock()
        out = NullControllerAdapter()
        sched = Scheduler(output=out, clock=clock, max_hold_ms=MAX_HOLD_MS)
        for seq in range(1, 5001):
            sched.submit(walk_right(1000.0, seq=seq))
        clock.advance(TICK_MS)
        sched.tick()
        assert out.states[-1].left_stick == pytest.approx((1.0, 0.0))
        assert len(sched.drain_events()) <= 4096


class TestSubmitNeverRaises:
    def test_a_chunk_that_lies_about_its_duration_cannot_escape(self) -> None:
        """Duration must come from the trusted resolved keyframes, not from re-reading a
        property on the untrusted object after validation."""
        class LyingChunk(ActionChunk):
            @property
            def duration_ms(self) -> float:
                raise RuntimeError("gotcha")

        sched = Scheduler(output=NullControllerAdapter(), clock=ManualClock(),
                          max_hold_ms=MAX_HOLD_MS)
        bad = LyingChunk(keyframes=(Keyframe(t_ms=0.0), Keyframe(t_ms=100.0)))
        assert sched.submit(bad) is False

    def test_deadman_still_fires_for_an_accepted_chunk(self) -> None:
        """Guards against 'fix the lying-duration bug by defaulting duration to infinity'."""
        clock = ManualClock()
        out = NullControllerAdapter()
        sched = Scheduler(output=out, clock=clock, max_hold_ms=MAX_HOLD_MS)
        sched.submit(walk_right(100.0))
        run_until(sched, clock, 100.0 + MAX_HOLD_MS + TICK_MS * 2)
        assert out.states[-1] == NEUTRAL


class TestThreadLifecycle:
    def test_stop_then_start_does_not_strand_a_running_loop(self) -> None:
        """stop() sets the stop flag outside the lifecycle lock, so a concurrent start()
        can clear it and spawn a replacement that never sees the request. stop() then
        joins a thread that will never exit."""
        out = NullControllerAdapter()
        sched = Scheduler(output=out, clock=SessionClock(), max_hold_ms=MAX_HOLD_MS)
        thread = SchedulerThread(sched)
        for _ in range(10):
            thread.start()
            thread.stop()
            assert not thread.is_alive()

    def test_stop_is_idempotent(self) -> None:
        sched = Scheduler(output=NullControllerAdapter(), clock=SessionClock(),
                          max_hold_ms=MAX_HOLD_MS)
        thread = SchedulerThread(sched)
        thread.start()
        thread.stop()
        thread.stop()
        assert not thread.is_alive()

    def test_releases_the_pad_on_a_base_exception(self) -> None:
        """`except Exception` misses SystemExit and KeyboardInterrupt. Release must be in
        a finally, so that no exit path leaves an input held."""
        class ExitingOutput(NullControllerAdapter):
            calls = 0

            def set_state(self, state: PadState) -> None:
                ExitingOutput.calls += 1
                if ExitingOutput.calls == 3:
                    raise SystemExit("shutting down")
                super().set_state(state)

        out = ExitingOutput()
        sched = Scheduler(output=out, clock=SessionClock(), max_hold_ms=MAX_HOLD_MS)
        thread = SchedulerThread(sched)
        thread.start()
        time.sleep(0.2)
        thread.stop()
        assert out.states[-1] == NEUTRAL
