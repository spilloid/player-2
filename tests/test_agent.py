"""Contract for player2.agent -- the seam where perception becomes intent.

This is the boundary the whole architecture exists to protect. Everything below it (capture,
scheduling, controller output) is game-agnostic machinery that must keep working while a
model is slow, wrong, or absent. Everything above it is cognition, which is allowed to be
none of those things.

Two properties matter more than any feature here:

  * A slow policy must not stall the hands. "The agent keeps moving while it thinks" is the
    difference between a teammate and a puppet, and it is made falsifiable below rather than
    asserted in a docstring.
  * The model's history must be what was EXECUTED, not what was proposed. Proposals get
    preempted, rejected, and overridden. A model reasoning from its own intentions rather
    than from what the game received will diverge from reality and never notice.

There is one subtlety worth stating plainly: `Scheduler.drain_events()` clears as it reads,
so two consumers would each silently receive half the events. The loop is therefore the
single drainer and forwards events to the recorder, rather than both polling the scheduler.
"""

from __future__ import annotations

import threading
import time
from dataclasses import FrozenInstanceError

import pytest

from player2.agent.base import IFastPolicy, Observation
from player2.agent.fast_stub import ScriptedPolicy
from player2.agent.loop import AgentLoop
from player2.clock import SessionClock
from player2.contracts import ActionChunk, Keyframe
from player2.control.null import NullControllerAdapter
from player2.control.scheduler import Scheduler, SchedulerThread
from player2.video.fake import FakeVideoSource


def walk(seq: int = 0, ms: float = 200.0) -> ActionChunk:
    return ActionChunk(
        keyframes=(Keyframe(t_ms=0.0, left_stick=(1.0, 0.0)), Keyframe(t_ms=ms)),
        decision_seq=seq,
    )


class RecordingPolicy:
    """Captures the observations it is handed, so the loop's contract can be inspected."""

    def __init__(self, result: ActionChunk | None = None) -> None:
        self.seen: list[Observation] = []
        self.result = result
        self.lock = threading.Lock()

    def propose(self, observation: Observation) -> ActionChunk | None:
        with self.lock:
            self.seen.append(observation)
        return self.result


def build(policy: object, *, clock: object | None = None) -> tuple[AgentLoop, Scheduler,
                                                                   FakeVideoSource,
                                                                   NullControllerAdapter]:
    the_clock = clock if clock is not None else SessionClock()
    output = NullControllerAdapter()
    scheduler = Scheduler(output=output, clock=the_clock, max_hold_ms=250.0)  # type: ignore[arg-type]
    video = FakeVideoSource(clock=the_clock)  # type: ignore[arg-type]
    video.start()
    loop = AgentLoop(policy=policy, video=video, scheduler=scheduler,  # type: ignore[arg-type]
                     clock=the_clock, goal="test goal")  # type: ignore[arg-type]
    return loop, scheduler, video, output


class TestPolicyInterface:
    def test_scripted_policy_satisfies_the_interface(self) -> None:
        assert isinstance(ScriptedPolicy([walk()]), IFastPolicy)

    def test_scripted_policy_cycles_its_chunks(self) -> None:
        first, second = walk(ms=100.0), walk(ms=200.0)
        policy = ScriptedPolicy([first, second])
        observation = Observation(frames=(), goal=None, history=(),
                                  observation_cutoff_ms=0.0, deadline_ms=100.0,
                                  epoch=0, decision_seq=0)
        assert policy.propose(observation) is first
        assert policy.propose(observation) is second
        assert policy.propose(observation) is first

    def test_an_empty_script_proposes_nothing(self) -> None:
        observation = Observation(frames=(), goal=None, history=(),
                                  observation_cutoff_ms=0.0, deadline_ms=0.0,
                                  epoch=0, decision_seq=0)
        assert ScriptedPolicy([]).propose(observation) is None


class TestObservation:
    def test_carries_what_the_model_needs_to_reason_causally(self) -> None:
        """observation_cutoff_ms says WHEN what it saw was true; deadline_ms says how long
        it has. Without the first, a model cannot tell fresh frames from stale ones."""
        observation = Observation(frames=(), goal="do the thing", history=(),
                                  observation_cutoff_ms=123.0, deadline_ms=456.0,
                                  epoch=2, decision_seq=7)
        assert observation.observation_cutoff_ms == 123.0
        assert observation.deadline_ms == 456.0
        assert (observation.epoch, observation.decision_seq) == (2, 7)

    def test_is_frozen(self) -> None:
        observation = Observation(frames=(), goal=None, history=(),
                                  observation_cutoff_ms=0.0, deadline_ms=0.0,
                                  epoch=0, decision_seq=0)
        with pytest.raises(FrozenInstanceError):
            observation.goal = "changed"  # type: ignore[misc]


class TestLoopFeedsThePolicy:
    def test_hands_the_policy_recent_frames(self) -> None:
        policy = RecordingPolicy()
        loop, scheduler, video, _ = build(policy)
        video.emit(3)
        loop.start()
        try:
            deadline = time.time() + 3.0
            while time.time() < deadline and not policy.seen:
                time.sleep(0.01)
        finally:
            loop.stop()
        assert policy.seen, "the policy was never called"
        assert policy.seen[0].frames, "the policy was given no frames"

    def test_passes_the_goal_through_untouched(self) -> None:
        """The runtime never interprets a goal. It is a string it carries to the model."""
        policy = RecordingPolicy()
        loop, _, video, _ = build(policy)
        video.emit(1)
        loop.start()
        try:
            deadline = time.time() + 3.0
            while time.time() < deadline and not policy.seen:
                time.sleep(0.01)
        finally:
            loop.stop()
        assert policy.seen[0].goal == "test goal"

    def test_observation_cutoff_matches_the_newest_frame(self) -> None:
        policy = RecordingPolicy()
        loop, _, video, _ = build(policy)
        video.emit(2)
        loop.start()
        try:
            deadline = time.time() + 3.0
            while time.time() < deadline and not policy.seen:
                time.sleep(0.01)
        finally:
            loop.stop()
        observation = policy.seen[0]
        assert observation.observation_cutoff_ms == max(f.session_ms for f in observation.frames)

    def test_waits_for_frames_rather_than_inventing_an_empty_observation(self) -> None:
        """Asking a vision model what to do without giving it any vision is worse than
        waiting: it produces confident output about nothing."""
        policy = RecordingPolicy()
        loop, _, _video, _ = build(policy)
        loop.start()
        time.sleep(0.3)
        loop.stop()
        assert all(o.frames for o in policy.seen)


class TestLoopSubmitsIntent:
    def test_submits_a_proposed_chunk_to_the_scheduler(self) -> None:
        loop, scheduler, video, output = build(ScriptedPolicy([walk()]))
        video.emit(1)
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
        assert any(s.left_stick[0] > 0.5 for s in output.states)

    def test_decision_sequence_strictly_increases(self) -> None:
        """The scheduler rejects a stale seq, so a loop that reuses numbers would have every
        proposal after the first silently dropped."""
        policy = RecordingPolicy(result=walk())
        loop, scheduler, video, _ = build(policy)
        video.emit(1)
        loop.start()
        try:
            deadline = time.time() + 3.0
            while time.time() < deadline and len(policy.seen) < 3:
                video.emit(1)
                time.sleep(0.01)
        finally:
            loop.stop()
        seqs = [o.decision_seq for o in policy.seen]
        assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)

    def test_stamps_the_current_scheduler_epoch(self) -> None:
        """A proposal authored before a human takeover must be rejected when it lands. That
        only works if the loop stamps the epoch it observed."""
        policy = RecordingPolicy(result=walk())
        loop, scheduler, video, _ = build(policy)
        scheduler.take_controller()
        scheduler.return_controller()
        video.emit(1)
        loop.start()
        try:
            deadline = time.time() + 3.0
            while time.time() < deadline and not policy.seen:
                time.sleep(0.01)
        finally:
            loop.stop()
        assert policy.seen[0].epoch == scheduler.epoch

    def test_a_policy_returning_none_submits_nothing(self) -> None:
        loop, scheduler, video, output = build(RecordingPolicy(result=None))
        video.emit(1)
        loop.start()
        time.sleep(0.3)
        loop.stop()
        assert loop.stats.chunks_accepted == 0


class TestLoopIsolatesCognitionFromTheHands:
    def test_a_slow_policy_does_not_stall_the_scheduler(self) -> None:
        """THE falsifiable claim. A policy that takes a full second per decision -- which a
        remote vision model absolutely will -- must not cost the scheduler a single tick."""
        class SlowPolicy:
            def propose(self, observation: Observation) -> ActionChunk | None:
                time.sleep(1.0)
                return None

        loop, scheduler, video, output = build(SlowPolicy())
        video.emit(1)
        thread = SchedulerThread(scheduler)
        thread.start()
        loop.start()
        try:
            time.sleep(0.5)
            ticks = len(output.states)
        finally:
            loop.stop()
            thread.stop()
        assert ticks > 20, f"scheduler only ticked {ticks} times while the policy was slow"

    def test_a_policy_that_raises_does_not_kill_the_loop(self) -> None:
        """A model will produce malformed output eventually. Losing the agent's mind for the
        rest of the session because of one bad response is not acceptable."""
        class FlakyPolicy:
            def __init__(self) -> None:
                self.calls = 0

            def propose(self, observation: Observation) -> ActionChunk | None:
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("model returned nonsense")
                return None

        policy = FlakyPolicy()
        loop, _, video, _ = build(policy)
        video.emit(1)
        loop.start()
        try:
            deadline = time.time() + 3.0
            while time.time() < deadline and policy.calls < 3:
                time.sleep(0.01)
        finally:
            loop.stop()
        assert policy.calls >= 2, "the loop died on the first policy failure"
        assert loop.stats.policy_errors >= 1

    def test_a_policy_error_detail_is_not_silently_discarded(self) -> None:
        """`_decide()` used to catch the policy's exception, count it, and throw the exception
        itself away -- a policy_errors=2 in the final stats line carried no information about
        what actually failed. This is distinct from `last_error` (worker-thread death); a single
        flaky decision must not be mistaken for the whole loop dying, so it gets its own field."""
        class AlwaysRaises:
            def propose(self, observation: Observation) -> ActionChunk | None:
                raise RuntimeError("distinctive failure detail")

        loop, _, video, _ = build(AlwaysRaises())
        video.emit(1)
        loop.start()
        try:
            deadline = time.time() + 3.0
            while time.time() < deadline and loop.stats.policy_errors < 1:
                time.sleep(0.01)
        finally:
            loop.stop()
        stats = loop.stats
        assert stats.policy_errors >= 1
        assert stats.last_policy_error is not None
        assert "distinctive failure detail" in stats.last_policy_error
        assert stats.last_error is None, "a routine policy error must not read as worker death"

    def test_a_policy_error_with_a_broken_str_does_not_kill_the_loop(self) -> None:
        """Formatting the caught exception (`str(error)`) must not itself be able to raise --
        that would let ONE malformed exception escape `_decide()`'s own handler, reach `_run()`'s
        fatal handler, and kill cognition entirely. Exactly the failure mode this fix exists to
        prevent, just reached through the detail-capturing code instead of around it."""
        class BrokenStr(RuntimeError):
            def __str__(self) -> str:
                raise ValueError("broken __str__")

        class PoisonPolicy:
            def propose(self, observation: Observation) -> ActionChunk | None:
                raise BrokenStr("irrelevant")

        loop, _, video, _ = build(PoisonPolicy())
        video.emit(1)
        loop.start()
        try:
            deadline = time.time() + 3.0
            while time.time() < deadline and loop.stats.policy_errors < 1:
                time.sleep(0.01)
        finally:
            loop.stop()
        stats = loop.stats
        assert stats.policy_errors >= 1, (
            "the polling loop timed out, meaning the worker thread died from the broken __str__ "
            "before it could record even one policy error"
        )
        assert stats.last_policy_error is not None
        assert stats.last_error is None, "a poisoned policy exception must not read as worker death"

    def test_an_invalid_chunk_is_rejected_without_stopping_the_loop(self) -> None:
        loop, scheduler, video, _ = build(ScriptedPolicy([ActionChunk(keyframes=())]))
        video.emit(1)
        loop.start()
        time.sleep(0.3)
        loop.stop()
        assert loop.stats.chunks_rejected >= 1

    def test_stop_is_idempotent_and_joins_the_thread(self) -> None:
        loop, _, video, _ = build(RecordingPolicy())
        video.emit(1)
        loop.start()
        loop.stop()
        loop.stop()
        assert loop.is_running is False


class TestHistoryIsWhatHappenedNotWhatWasIntended:
    def test_history_carries_executed_scheduler_events(self) -> None:
        """A model that believes its proposals were executed will explain away every
        surprise. It must see the preemptions, rejections and takeovers instead."""
        policy = RecordingPolicy(result=walk())
        loop, scheduler, video, _ = build(policy)
        video.emit(1)
        scheduler.take_controller()
        scheduler.return_controller()
        loop.start()
        try:
            deadline = time.time() + 3.0
            while time.time() < deadline and len(policy.seen) < 2:
                video.emit(1)
                time.sleep(0.02)
        finally:
            loop.stop()
        kinds = {event.kind for o in policy.seen for event in o.history}
        assert kinds, "the policy was given no execution history at all"

    def test_the_loop_drains_scheduler_events(self) -> None:
        """drain_events() clears as it reads, so exactly one component may poll it. The loop
        does, and forwards to the recorder; if the recorder polled independently, each would
        silently receive an arbitrary half of the execution record."""
        policy = RecordingPolicy(result=walk())
        loop, scheduler, video, _ = build(policy)
        video.emit(1)
        scheduler.take_controller()
        loop.start()
        try:
            deadline = time.time() + 3.0
            while time.time() < deadline and loop.stats.events_seen == 0:
                time.sleep(0.01)
        finally:
            loop.stop()
        assert loop.stats.events_seen >= 1, "the loop never drained the scheduler's events"

    def test_forwards_events_to_a_recorder(self, tmp_path: object) -> None:
        """Because the loop is the only drainer, it owes the recorder those events."""
        from player2.record.writer import SessionRecorder

        recorder = SessionRecorder(root=tmp_path, clock=SessionClock())  # type: ignore[arg-type]
        recorder.start()
        output = NullControllerAdapter()
        clock = SessionClock()
        scheduler = Scheduler(output=output, clock=clock, max_hold_ms=250.0)
        video = FakeVideoSource(clock=clock)
        video.start()
        video.emit(1)
        loop = AgentLoop(policy=ScriptedPolicy([walk()]), video=video, scheduler=scheduler,
                         clock=clock, goal=None, recorder=recorder)
        scheduler.take_controller()
        loop.start()
        try:
            deadline = time.time() + 3.0
            while time.time() < deadline and loop.stats.events_seen == 0:
                time.sleep(0.01)
        finally:
            loop.stop()
            recorder.stop()
        events = (recorder.directory / "events.jsonl").read_text()
        assert events.strip(), "scheduler events never reached the recorder"
