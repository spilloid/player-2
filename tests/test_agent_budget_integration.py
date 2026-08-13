"""Contract for AgentLoop's optional budget governor hook.

Deliberately a separate file from test_agent.py rather than an edit to it: every test already
in that file must keep passing completely unmodified, because governor=None (the default) has
to be byte-for-byte the pre-existing behavior. This file only adds new coverage for the case
where a governor is supplied.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from player2.agent.base import Observation
from player2.agent.budget import BudgetDecision, BudgetLevel
from player2.agent.loop import AgentLoop
from player2.clock import SessionClock
from player2.contracts import ActionChunk, Keyframe
from player2.control.null import NullControllerAdapter
from player2.control.scheduler import Scheduler
from player2.video.fake import FakeVideoSource


def walk(seq: int = 0, ms: float = 200.0) -> ActionChunk:
    return ActionChunk(
        keyframes=(Keyframe(t_ms=0.0, left_stick=(1.0, 0.0)), Keyframe(t_ms=ms)),
        decision_seq=seq,
    )


class RecordingPolicy:
    def __init__(self, result: ActionChunk | None = None) -> None:
        self.seen: list[Observation] = []
        self.result = result
        self.lock = threading.Lock()

    def propose(self, observation: Observation) -> ActionChunk | None:
        with self.lock:
            self.seen.append(observation)
        return self.result


@dataclass
class FixedGovernor:
    """Always return the same scripted decision, so the loop's use of it is directly visible."""

    decision: BudgetDecision

    def decide(self) -> BudgetDecision:
        return self.decision


def build(policy: object, governor: object | None = None) -> tuple[
    AgentLoop, Scheduler, FakeVideoSource, NullControllerAdapter,
]:
    clock = SessionClock()
    output = NullControllerAdapter()
    scheduler = Scheduler(output=output, clock=clock, max_hold_ms=250.0)  # type: ignore[arg-type]
    video = FakeVideoSource(clock=clock)  # type: ignore[arg-type]
    video.start()
    loop = AgentLoop(policy=policy, video=video, scheduler=scheduler,  # type: ignore[arg-type]
                     clock=clock, goal="test goal",
                     min_interval_ms=50.0, frames_per_observation=4,
                     governor=governor)  # type: ignore[arg-type]
    return loop, scheduler, video, output


class TestNoGovernorIsUnchangedBehavior:
    def test_uses_the_constructor_frame_count_when_no_governor_is_given(self) -> None:
        policy = RecordingPolicy()
        loop, _, video, _ = build(policy, governor=None)
        video.emit(4)
        loop.start()
        try:
            deadline = time.time() + 3.0
            while time.time() < deadline and not policy.seen:
                time.sleep(0.01)
        finally:
            loop.stop()
        assert policy.seen
        assert len(policy.seen[0].frames) <= 4


class TestDegradedGovernor:
    def test_observation_deadline_reflects_the_governors_lengthened_interval(self) -> None:
        """A degraded cycle that doubles the interval but still stamps Observation.deadline_ms
        from the constructor's fixed min_interval_ms would tell the model it has far less time
        than it actually does -- the model reasons from observation_cutoff_ms/deadline_ms (see
        model_schema.observation_to_prompt), so a stale deadline here is a lie to the model,
        not just a cosmetic accounting gap."""
        policy = RecordingPolicy()
        governor = FixedGovernor(BudgetDecision(
            level=BudgetLevel.DEGRADED, min_interval_ms=500.0,
            frames_per_observation=1, should_propose=True,
        ))
        loop, _, video, _ = build(policy, governor=governor)
        video.emit(4)
        loop.start()
        try:
            deadline = time.time() + 3.0
            while time.time() < deadline and not policy.seen:
                time.sleep(0.01)
        finally:
            loop.stop()
        observation = policy.seen[0]
        budget_ms = observation.deadline_ms - observation.observation_cutoff_ms
        assert budget_ms > 100.0, (
            f"expected a budget close to the governor's 500ms interval, got {budget_ms}ms -- "
            "looks like deadline_ms was stamped from the constructor's fixed min_interval_ms"
        )

    def test_uses_the_governors_frame_count_instead_of_the_constructor_default(self) -> None:
        policy = RecordingPolicy()
        governor = FixedGovernor(BudgetDecision(
            level=BudgetLevel.DEGRADED, min_interval_ms=50.0,
            frames_per_observation=1, should_propose=True,
        ))
        loop, _, video, _ = build(policy, governor=governor)
        video.emit(4)
        loop.start()
        try:
            deadline = time.time() + 3.0
            while time.time() < deadline and not policy.seen:
                time.sleep(0.01)
        finally:
            loop.stop()
        assert policy.seen
        assert len(policy.seen[0].frames) <= 1

    def test_still_proposes_when_degraded_not_exhausted(self) -> None:
        governor = FixedGovernor(BudgetDecision(
            level=BudgetLevel.DEGRADED, min_interval_ms=30.0,
            frames_per_observation=2, should_propose=True,
        ))
        loop, scheduler, video, output = build(RecordingPolicy(result=walk()), governor=governor)
        video.emit(2)
        from player2.control.scheduler import SchedulerThread
        thread = SchedulerThread(scheduler)
        thread.start()
        loop.start()
        try:
            deadline = time.time() + 3.0
            while time.time() < deadline and loop.stats.chunks_accepted == 0:
                time.sleep(0.01)
        finally:
            loop.stop()
            thread.stop()
        assert loop.stats.chunks_accepted >= 1


class TestExhaustedGovernor:
    def test_never_calls_propose_while_exhausted(self) -> None:
        policy = RecordingPolicy(result=walk())
        governor = FixedGovernor(BudgetDecision(
            level=BudgetLevel.EXHAUSTED, min_interval_ms=30.0,
            frames_per_observation=1, should_propose=False,
        ))
        loop, _, video, _ = build(policy, governor=governor)
        video.emit(4)
        loop.start()
        time.sleep(0.3)
        loop.stop()
        assert not policy.seen

    def test_still_records_frames_while_exhausted(self) -> None:
        """Budget throttling is a cognition-side concern; perception should keep running so
        the loop can resume decisions instantly once the rate recovers, and so a recorder
        attached to the loop still captures footage during the pause."""
        policy = RecordingPolicy(result=walk())
        governor = FixedGovernor(BudgetDecision(
            level=BudgetLevel.EXHAUSTED, min_interval_ms=30.0,
            frames_per_observation=1, should_propose=False,
        ))
        loop, _, video, _ = build(policy, governor=governor)
        video.emit(4)
        loop.start()
        deadline = time.time() + 3.0
        while (time.time() < deadline and loop.stats.events_seen == 0
               and video.stats.frames_captured == 0):
            time.sleep(0.01)
        time.sleep(0.2)
        loop.stop()
        assert video.stats.frames_captured > 0

    def test_counts_skipped_cycles_in_stats(self) -> None:
        policy = RecordingPolicy(result=walk())
        governor = FixedGovernor(BudgetDecision(
            level=BudgetLevel.EXHAUSTED, min_interval_ms=20.0,
            frames_per_observation=1, should_propose=False,
        ))
        loop, _, video, _ = build(policy, governor=governor)
        video.emit(4)
        loop.start()
        deadline = time.time() + 1.0
        while time.time() < deadline and loop.stats.budget_skips == 0:
            time.sleep(0.01)
        loop.stop()
        assert loop.stats.budget_skips > 0
        assert not policy.seen

    def test_the_scheduler_deadman_still_releases_the_pad(self) -> None:
        """The governor invents no new release mechanism: once it stops proposals, the
        scheduler's existing deadman is what returns the pad to neutral -- the same failsafe,
        reused, exactly as CARRYOVER describes them as belonging together conceptually."""
        governor = FixedGovernor(BudgetDecision(
            level=BudgetLevel.EXHAUSTED, min_interval_ms=20.0,
            frames_per_observation=1, should_propose=False,
        ))
        clock = SessionClock()
        output = NullControllerAdapter()
        scheduler = Scheduler(output=output, clock=clock, max_hold_ms=80.0)  # type: ignore[arg-type]
        from player2.control.scheduler import SchedulerThread
        thread = SchedulerThread(scheduler)
        thread.start()
        # Prime the pad with real motion submitted directly, bypassing the loop/policy, so
        # the deadman has something to release.
        scheduler.submit(walk(seq=0, ms=40.0))
        time.sleep(0.05)
        video = FakeVideoSource(clock=clock)  # type: ignore[arg-type]
        video.start()
        loop = AgentLoop(policy=RecordingPolicy(result=walk()), video=video,  # type: ignore[arg-type]
                         scheduler=scheduler, clock=clock, min_interval_ms=20.0,
                         frames_per_observation=1, governor=governor)  # type: ignore[arg-type]
        video.emit(2)
        loop.start()
        try:
            deadline = time.time() + 1.0
            while time.time() < deadline and output.states[-1].left_stick != (0.0, 0.0):
                time.sleep(0.01)
        finally:
            loop.stop()
            thread.stop()
        assert output.states[-1].left_stick == (0.0, 0.0)


class TestGovernorRecoversAcrossCycles:
    def test_a_governor_that_changes_its_mind_is_consulted_every_cycle(self) -> None:
        """decide() must be called fresh each cycle, not cached at loop start -- otherwise a
        governor that later recovers to NORMAL could never bring cadence back up."""

        class TogglingGovernor:
            def __init__(self) -> None:
                self.calls = 0
                self.lock = threading.Lock()

            def decide(self) -> BudgetDecision:
                with self.lock:
                    self.calls += 1
                    exhausted = self.calls <= 3
                return BudgetDecision(
                    level=BudgetLevel.EXHAUSTED if exhausted else BudgetLevel.NORMAL,
                    min_interval_ms=15.0, frames_per_observation=1,
                    should_propose=not exhausted,
                )

        policy = RecordingPolicy(result=walk())
        governor = TogglingGovernor()
        loop, _, video, _ = build(policy, governor=governor)
        video.emit(4)
        loop.start()
        try:
            deadline = time.time() + 3.0
            while time.time() < deadline and not policy.seen:
                time.sleep(0.01)
        finally:
            loop.stop()
        assert policy.seen, "the loop never recovered from an exhausted governor"
