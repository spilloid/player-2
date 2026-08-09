"""ViGEm-backed virtual Xbox controller output."""

from __future__ import annotations

from typing import Any

from player2.contracts import NEUTRAL, Button, PadState

BUTTON_MAP: dict[Button, str] = {
    Button.A: "XUSB_GAMEPAD_A",
    Button.B: "XUSB_GAMEPAD_B",
    Button.X: "XUSB_GAMEPAD_X",
    Button.Y: "XUSB_GAMEPAD_Y",
    Button.LB: "XUSB_GAMEPAD_LEFT_SHOULDER",
    Button.RB: "XUSB_GAMEPAD_RIGHT_SHOULDER",
    Button.BACK: "XUSB_GAMEPAD_BACK",
    Button.START: "XUSB_GAMEPAD_START",
    Button.GUIDE: "XUSB_GAMEPAD_GUIDE",
    Button.LS: "XUSB_GAMEPAD_LEFT_THUMB",
    Button.RS: "XUSB_GAMEPAD_RIGHT_THUMB",
    Button.DPAD_UP: "XUSB_GAMEPAD_DPAD_UP",
    Button.DPAD_DOWN: "XUSB_GAMEPAD_DPAD_DOWN",
    Button.DPAD_LEFT: "XUSB_GAMEPAD_DPAD_LEFT",
    Button.DPAD_RIGHT: "XUSB_GAMEPAD_DPAD_RIGHT",
}


class ViGEmXboxAdapter:
    """Send complete states through ViGEm; stick y=+1 means up, matching XInput."""

    def __init__(self) -> None:
        """Load the Windows-only dependency only when a real controller is requested."""
        import vgamepad as vg  # type: ignore[import-untyped]

        self._vg: Any = vg
        self._pad: Any = vg.VX360Gamepad()
        self._buttons: dict[Button, Any] = {
            button: getattr(vg.XUSB_BUTTON, name) for button, name in BUTTON_MAP.items()
        }

    def set_state(self, state: PadState) -> None:
        """Set every channel before one update, preventing stale reports from persisting."""
        self._pad.reset()
        self._pad.left_joystick_float(
            x_value_float=state.left_stick[0], y_value_float=state.left_stick[1]
        )
        self._pad.right_joystick_float(
            x_value_float=state.right_stick[0], y_value_float=state.right_stick[1]
        )
        self._pad.left_trigger_float(value_float=state.left_trigger)
        self._pad.right_trigger_float(value_float=state.right_trigger)
        for button in state.buttons:
            self._pad.press_button(button=self._buttons[button])
        self._pad.update()

    def reset(self) -> None:
        """Release the full report and send it, because reset alone does not reach hardware."""
        self.set_state(NEUTRAL)
