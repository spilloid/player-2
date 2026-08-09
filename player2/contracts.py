"""Pure data contracts for controller actions, resolved input, and captured frames."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, cast

TICK_HZ = 120.0
TICK_MS = 1000.0 / TICK_HZ
MIN_PULSE_MS = 2.0 * TICK_MS
MAX_KEYFRAMES = 512
MAX_CHUNK_MS = 5000.0


class InvalidPadState(ValueError):
    """Report a controller state that could not safely be sent to a pad."""


class InvalidChunk(ValueError):
    """Report a malformed or unsafe action chunk before it reaches the scheduler."""


class InvalidFrame(ValueError):
    """Report capture metadata that cannot describe a valid video frame."""


class Button(StrEnum):
    """Name the buttons supported by the Xbox-shaped virtual controller."""

    A = "A"
    B = "B"
    X = "X"
    Y = "Y"
    LB = "LB"
    RB = "RB"
    BACK = "BACK"
    START = "START"
    GUIDE = "GUIDE"
    LS = "LS"
    RS = "RS"
    DPAD_UP = "DPAD_UP"
    DPAD_DOWN = "DPAD_DOWN"
    DPAD_LEFT = "DPAD_LEFT"
    DPAD_RIGHT = "DPAD_RIGHT"


Stick = tuple[float, float]


def _finite(value: object) -> bool:
    """Check numeric finiteness without letting hostile values escape validation."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(float(value))
    except (OverflowError, TypeError, ValueError):
        return False


def _stick(value: object) -> Stick:
    """Copy and validate a stick so caller-owned sequences cannot mutate state later."""
    try:
        components: tuple[object, ...] = tuple(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise InvalidPadState("stick must be an iterable of two numbers") from None
    if len(components) != 2:
        raise InvalidPadState("stick must contain two numbers")
    first, second = components
    if (not _finite(first) or not _finite(second) or
            not -1.0 <= float(cast(int | float, first)) <= 1.0 or
            not -1.0 <= float(cast(int | float, second)) <= 1.0):
        raise InvalidPadState("stick values out of range")
    return (float(cast(int | float, first)), float(cast(int | float, second)))


def _trigger(value: object, name: str) -> float:
    """Validate an analog value before comparisons can raise on untrusted input."""
    if not _finite(value):
        raise InvalidPadState(f"invalid {name}")
    numeric = float(cast(int | float, value))
    if not 0.0 <= numeric <= 1.0:
        raise InvalidPadState(f"invalid {name}")
    return numeric


@dataclass(frozen=True)
class PadState:
    """Represent a complete validated pad snapshot, including explicit button release."""

    left_stick: Stick = (0.0, 0.0)
    right_stick: Stick = (0.0, 0.0)
    left_trigger: float = 0.0
    right_trigger: float = 0.0
    buttons: frozenset[Button] = frozenset()

    def __post_init__(self) -> None:
        """Reject out-of-range values because clamping would hide controller model bugs."""
        left_stick = _stick(self.left_stick)
        right_stick = _stick(self.right_stick)
        left_trigger = _trigger(self.left_trigger, "left_trigger")
        right_trigger = _trigger(self.right_trigger, "right_trigger")
        try:
            buttons = frozenset(self.buttons)
        except (TypeError, ValueError):
            raise InvalidPadState("unknown button") from None
        if not all(isinstance(button, Button) for button in buttons):
            raise InvalidPadState("unknown button")
        object.__setattr__(self, "left_stick", left_stick)
        object.__setattr__(self, "right_stick", right_stick)
        object.__setattr__(self, "left_trigger", left_trigger)
        object.__setattr__(self, "right_trigger", right_trigger)
        object.__setattr__(self, "buttons", buttons)


NEUTRAL = PadState()


_MISSING = object()


@dataclass(frozen=True, init=False)
class Keyframe:
    """Describe sparse authoring input; omitted channels intentionally mean inherit."""

    t_ms: float
    left_stick: Stick | None = None
    right_stick: Stick | None = None
    left_trigger: float | None = None
    right_trigger: float | None = None
    buttons: frozenset[Button] | None = None
    _left_stick_provided: bool = field(default=False, repr=False, compare=False)
    _right_stick_provided: bool = field(default=False, repr=False, compare=False)
    _left_trigger_provided: bool = field(default=False, repr=False, compare=False)
    _right_trigger_provided: bool = field(default=False, repr=False, compare=False)
    _buttons_provided: bool = field(default=False, repr=False, compare=False)

    def __init__(
        self,
        t_ms: float,
        left_stick: Stick | None | object = _MISSING,
        right_stick: Stick | None | object = _MISSING,
        left_trigger: float | None | object = _MISSING,
        right_trigger: float | None | object = _MISSING,
        buttons: frozenset[Button] | None | object = _MISSING,
    ) -> None:
        """Remember explicit nulls so malformed wire values cannot mean inherit."""
        object.__setattr__(self, "t_ms", t_ms)
        object.__setattr__(self, "left_stick", None if left_stick is _MISSING else left_stick)
        object.__setattr__(self, "right_stick", None if right_stick is _MISSING else right_stick)
        object.__setattr__(self, "left_trigger", None if left_trigger is _MISSING else left_trigger)
        object.__setattr__(
            self, "right_trigger", None if right_trigger is _MISSING else right_trigger
        )
        object.__setattr__(self, "buttons", None if buttons is _MISSING else buttons)
        object.__setattr__(self, "_left_stick_provided", left_stick is not _MISSING)
        object.__setattr__(self, "_right_stick_provided", right_stick is not _MISSING)
        object.__setattr__(self, "_left_trigger_provided", left_trigger is not _MISSING)
        object.__setattr__(self, "_right_trigger_provided", right_trigger is not _MISSING)
        object.__setattr__(self, "_buttons_provided", buttons is not _MISSING)


@dataclass(frozen=True)
class ActionChunk:
    """Bundle sparse keyframes with provenance used to reject stale decisions."""

    keyframes: tuple[Keyframe, ...]
    decision_seq: int = 0
    epoch: int = 0
    observation_cutoff_ms: float | None = None

    @property
    def duration_ms(self) -> float:
        """Return the final keyframe time, the chunk's authored duration."""
        return self.keyframes[-1].t_ms if self.keyframes else 0.0


@dataclass(frozen=True)
class ResolvedKeyframe:
    """Pair each keyframe time with the complete state the scheduler can sample."""

    t_ms: float
    state: PadState


@dataclass(frozen=True)
class Frame:
    """Carry a captured image and its timestamps without inventing absent hardware time.

    `data` is deliberately opaque. Capture backends produce numpy arrays, and narrowing this
    to `bytes` would force a conversion of every frame at capture rate purely to satisfy an
    annotation -- roughly 16MB per frame at a modern desktop resolution. `pixel_format`
    describes how to read the buffer instead, which keeps this module stdlib-only and lets
    a future backend hand over a GPU handle without changing the contract.

    `source_ms` is the capture API's own timestamp mapped into session time, or None where
    the backend does not expose one. Substituting arrival time would record queue latency
    while claiming to record acquisition time, which silently misaligns the whole dataset.
    """

    seq: int
    session_ms: float
    source_ms: float | None
    width: int
    height: int
    pixel_format: str
    data: object

    def __post_init__(self) -> None:
        """Validate metadata so recordings cannot contain impossible frame provenance."""
        if (not isinstance(self.seq, int) or isinstance(self.seq, bool) or
                self.seq < 0):
            raise InvalidFrame("seq must be non-negative")
        if not _finite(self.session_ms) or (self.source_ms is not None and
                                            not _finite(self.source_ms)):
            raise InvalidFrame("timestamps must be finite")
        if (not isinstance(self.width, int) or not isinstance(self.height, int) or
                self.width <= 0 or self.height <= 0):
            raise InvalidFrame("dimensions must be positive integers")
        if not isinstance(self.pixel_format, str) or not self.pixel_format:
            raise InvalidFrame("pixel_format must not be empty")


def _validate_chunk(chunk: ActionChunk) -> None:
    """Validate the whole chunk before resolving any field, preserving one safe error boundary."""
    if not isinstance(chunk, ActionChunk):
        raise InvalidChunk("expected an action chunk")
    if not isinstance(chunk.keyframes, tuple) or not chunk.keyframes:
        raise InvalidChunk("chunk must contain a keyframe")
    if len(chunk.keyframes) > MAX_KEYFRAMES:
        raise InvalidChunk("chunk contains too many keyframes")
    if (not isinstance(chunk.decision_seq, int) or isinstance(chunk.decision_seq, bool)
            or chunk.decision_seq < 0):
        raise InvalidChunk("decision_seq must be non-negative")
    if (not isinstance(chunk.epoch, int) or isinstance(chunk.epoch, bool)
            or chunk.epoch < 0):
        raise InvalidChunk("epoch must be non-negative")
    if (chunk.observation_cutoff_ms is not None and
            (not _finite(chunk.observation_cutoff_ms) or
             chunk.observation_cutoff_ms < 0.0)):
        raise InvalidChunk("invalid observation cutoff")
    previous = -math.inf
    for index, keyframe in enumerate(chunk.keyframes):
        # Each cause gets its own message. A model reads the rejection reason to correct its
        # next chunk, so telling it "times must be strictly increasing" when the real problem
        # is that the chunk started at t=50 sends it chasing a bug that isn't there.
        if not isinstance(keyframe, Keyframe):
            raise InvalidChunk("keyframes must be Keyframe instances")
        if not _finite(keyframe.t_ms):
            raise InvalidChunk("keyframe time must be a finite number")
        if keyframe.t_ms < 0.0:
            raise InvalidChunk("keyframe time must not be negative")
        if index == 0 and keyframe.t_ms != 0.0:
            raise InvalidChunk("first keyframe must be at t_ms=0")
        if keyframe.t_ms <= previous:
            raise InvalidChunk("keyframe times must be strictly increasing")
        if keyframe.t_ms > MAX_CHUNK_MS:
            raise InvalidChunk(f"chunk exceeds the {MAX_CHUNK_MS}ms action horizon")
        if (keyframe._left_stick_provided and keyframe.left_stick is None or
                keyframe._right_stick_provided and keyframe.right_stick is None or
                keyframe._left_trigger_provided and keyframe.left_trigger is None or
                keyframe._right_trigger_provided and keyframe.right_trigger is None or
                keyframe._buttons_provided and keyframe.buttons is None):
            raise InvalidChunk("keyframe fields cannot be null")
        previous = keyframe.t_ms
        try:
            PadState(left_stick=(keyframe.left_stick if keyframe.left_stick is not None
                                 else (0.0, 0.0)),
                     right_stick=(keyframe.right_stick if keyframe.right_stick is not None
                                  else (0.0, 0.0)),
                     left_trigger=(keyframe.left_trigger if keyframe.left_trigger is not None
                                   else 0.0),
                     right_trigger=(keyframe.right_trigger if keyframe.right_trigger is not None
                                    else 0.0),
                     buttons=(keyframe.buttons if keyframe.buttons is not None
                              else frozenset()))
        except (InvalidPadState, TypeError, ValueError, OverflowError):
            raise InvalidChunk("invalid keyframe state") from None


def resolve(chunk: ActionChunk) -> tuple[ResolvedKeyframe, ...]:
    """Resolve sparse input from neutral so every omitted channel is safely released at start."""
    _validate_chunk(chunk)
    state = NEUTRAL
    resolved: list[ResolvedKeyframe] = []
    button_start: dict[Button, float] = {}
    button_release: dict[Button, float] = {}
    for keyframe in chunk.keyframes:
        state = PadState(
            left_stick=state.left_stick if keyframe.left_stick is None else keyframe.left_stick,
            right_stick=(state.right_stick if keyframe.right_stick is None
                         else keyframe.right_stick),
            left_trigger=(state.left_trigger if keyframe.left_trigger is None
                          else keyframe.left_trigger),
            right_trigger=(state.right_trigger if keyframe.right_trigger is None
                           else keyframe.right_trigger),
            buttons=state.buttons if keyframe.buttons is None else keyframe.buttons,
        )
        for button in Button:
            was_down = button in (resolved[-1].state.buttons if resolved else NEUTRAL.buttons)
            is_down = button in state.buttons
            if was_down and not is_down:
                if keyframe.t_ms - button_start[button] < MIN_PULSE_MS:
                    raise InvalidChunk("button pulse or release gap is too short")
                button_release[button] = keyframe.t_ms
            elif not was_down and is_down:
                if (button in button_release and
                        keyframe.t_ms - button_release[button] < MIN_PULSE_MS):
                    raise InvalidChunk("button pulse or release gap is too short")
                button_start[button] = keyframe.t_ms
        resolved.append(ResolvedKeyframe(keyframe.t_ms, state))
    return tuple(resolved)


def _lerp(first: float, second: float, fraction: float) -> float:
    """Interpolate analog values between resolved neighboring keyframes."""
    return first + (second - first) * fraction


def sample(resolved: tuple[ResolvedKeyframe, ...], t_ms: float) -> PadState:
    """Sample resolved input, interpolating analog channels while stepping buttons."""
    if t_ms < 0.0:
        raise ValueError("sample time must be non-negative")
    if not resolved:
        raise ValueError("cannot sample an empty resolution")
    if t_ms <= resolved[0].t_ms:
        return resolved[0].state
    index = 0
    while index + 1 < len(resolved) and resolved[index + 1].t_ms <= t_ms:
        index += 1
    if index == len(resolved) - 1:
        return resolved[index].state
    before, after = resolved[index], resolved[index + 1]
    fraction = (t_ms - before.t_ms) / (after.t_ms - before.t_ms)
    return PadState(
        left_stick=(_lerp(before.state.left_stick[0], after.state.left_stick[0], fraction),
                     _lerp(before.state.left_stick[1], after.state.left_stick[1], fraction)),
        right_stick=(_lerp(before.state.right_stick[0], after.state.right_stick[0], fraction),
                      _lerp(before.state.right_stick[1], after.state.right_stick[1], fraction)),
        left_trigger=_lerp(before.state.left_trigger, after.state.left_trigger, fraction),
        right_trigger=_lerp(before.state.right_trigger, after.state.right_trigger, fraction),
        buttons=before.state.buttons,
    )


def chunk_to_dict(chunk: ActionChunk) -> dict[str, Any]:
    """Convert a chunk to JSON-compatible data while preserving omitted versus empty buttons."""
    keyframes: list[dict[str, Any]] = []
    for keyframe in chunk.keyframes:
        item: dict[str, Any] = {"t_ms": keyframe.t_ms}
        for name in ("left_stick", "right_stick", "left_trigger", "right_trigger"):
            value = getattr(keyframe, name)
            if value is not None:
                item[name] = list(value) if isinstance(value, tuple) else value
        if keyframe.buttons is not None:
            item["buttons"] = sorted(button.value for button in keyframe.buttons)
        keyframes.append(item)
    return {"keyframes": keyframes, "decision_seq": chunk.decision_seq,
            "epoch": chunk.epoch, "observation_cutoff_ms": chunk.observation_cutoff_ms}


def chunk_from_dict(payload: object) -> ActionChunk:
    """Decode wire data without collapsing absent buttons into an explicit release."""
    if not isinstance(payload, dict):
        raise InvalidChunk("malformed chunk payload")
    try:
        frames = payload["keyframes"]
        if not isinstance(frames, list):
            raise InvalidChunk("malformed chunk payload")
        keyframes = []
        for item in frames:
            if not isinstance(item, dict):
                raise InvalidChunk("malformed chunk payload")
            values: dict[str, Any] = {"t_ms": item["t_ms"]}
            for name in ("left_stick", "right_stick", "left_trigger", "right_trigger"):
                if name in item:
                    value = item[name]
                    values[name] = tuple(value) if name.endswith("stick") else value
            if "buttons" in item:
                if not isinstance(item["buttons"], list):
                    raise InvalidChunk("malformed chunk payload")
                values["buttons"] = frozenset(Button(button) for button in item["buttons"])
            keyframes.append(Keyframe(**values))
        chunk = ActionChunk(keyframes=tuple(keyframes),
                           decision_seq=payload.get("decision_seq", 0),
                           epoch=payload.get("epoch", 0),
                           observation_cutoff_ms=payload.get("observation_cutoff_ms"))
        resolve(chunk)
        return chunk
    except InvalidChunk:
        # Deliberately re-raised unchanged. The specific reason ("button pulse too short",
        # "keyframe times must be strictly increasing") is the feedback signal a model uses
        # to correct its next chunk; flattening it into a generic string teaches it nothing.
        raise
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        raise InvalidChunk("malformed chunk payload") from None
