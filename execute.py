"""
FILENAME: execute.py
PIPELINE: main.py -> execute.py -> capture.py -> execute.py -> main.py

This process parses ACTIONS from the VLM raw text, optionally simulates Win32 input,
and always returns a fresh screenshot from capture.py.

Key behavior changes in this version:
- focus(...) is recognized but inert (not executed, not forwarded, no cropping).
- Only executed actions are forwarded for annotation (marks match reality).
- eval() removed; calls are parsed via ast.literal_eval and dispatched directly.
- sandbox mode forces physical execution off.
"""
import ast
import ctypes
import ctypes.wintypes
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Final

_LDOWN: Final[int] = 0x0002
_LUP: Final[int] = 0x0004
_RDOWN: Final[int] = 0x0008
_RUP: Final[int] = 0x0010
_KEYUP: Final[int] = 2
_VK_RETURN: Final[int] = 0x0D
_VK_SHIFT: Final[int] = 0x10
_VK_SPACE: Final[int] = 0x20

_MOVE_STEPS: Final[int] = 20
_STEP_DELAY: Final[float] = 0.01
_CLICK_DELAY: Final[float] = 0.15
_CHAR_DELAY: Final[float] = 0.08
_WORD_DELAY: Final[float] = 0.15

CAPTURE_SCRIPT: Final[Path] = Path(__file__).parent / "capture.py"

_shcore: Final[ctypes.WinDLL] = ctypes.WinDLL("shcore", use_last_error=True)
_shcore.SetProcessDpiAwareness(2)
_user32: Final[ctypes.WinDLL] = ctypes.WinDLL("user32", use_last_error=True)
_screen_w: Final[int] = _user32.GetSystemMetrics(0)
_screen_h: Final[int] = _user32.GetSystemMetrics(1)

KNOWN_FUNCTIONS: Final[frozenset[str]] = frozenset(
    {"left_click", "right_click", "double_left_click", "drag", "type", "screenshot", "focus"}
)

PHYSICAL_EXECUTION_DEFAULT: Final[bool] = False
SANDBOX_DEFAULT: Final[bool] = False

def _to_px(v: int, dim: int) -> int:
    v = 0 if v < 0 else 1000 if v > 1000 else v
    return int((v / 1000) * dim)

def _cursor_pos() -> tuple[int, int]:
    pt = ctypes.wintypes.POINT()
    _user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y

def _smooth_move(tx: int, ty: int) -> None:
    sx, sy = _cursor_pos()
    dx, dy = tx - sx, ty - sy
    for i in range(_MOVE_STEPS + 1):
        t = i / _MOVE_STEPS
        t = t * t * (3.0 - 2.0 * t)
        _user32.SetCursorPos(int(sx + dx * t), int(sy + dy * t))
        time.sleep(_STEP_DELAY)

def _mouse_click(down: int, up: int) -> None:
    _user32.mouse_event(down, 0, 0, 0, 0)
    time.sleep(0.05)
    _user32.mouse_event(up, 0, 0, 0, 0)

def _key_tap(vk: int) -> None:
    _user32.keybd_event(vk, 0, 0, 0)
    time.sleep(0.02)
    _user32.keybd_event(vk, 0, _KEYUP, 0)

def _type_text(text: str) -> None:
    for ch in text:
        if ch == " ":
            _key_tap(_VK_SPACE)
            time.sleep(_WORD_DELAY)
            continue
        if ch == "\n":
            _key_tap(_VK_RETURN)
            time.sleep(_WORD_DELAY)
            continue
        vk = _user32.VkKeyScanW(ord(ch))
        if vk == -1:
            continue
        need_shift = bool((vk >> 8) & 1)
        if need_shift:
            _user32.keybd_event(_VK_SHIFT, 0, 0, 0)
            time.sleep(0.01)
        _key_tap(vk & 0xFF)
        if need_shift:
            _user32.keybd_event(_VK_SHIFT, 0, _KEYUP, 0)
        time.sleep(_CHAR_DELAY)

def _do_left_click(x: int, y: int) -> None:
    _smooth_move(_to_px(x, _screen_w), _to_px(y, _screen_h))
    time.sleep(_CLICK_DELAY)
    _mouse_click(_LDOWN, _LUP)

def _do_right_click(x: int, y: int) -> None:
    _smooth_move(_to_px(x, _screen_w), _to_px(y, _screen_h))
    time.sleep(_CLICK_DELAY)
    _mouse_click(_RDOWN, _RUP)

def _do_double_left_click(x: int, y: int) -> None:
    _smooth_move(_to_px(x, _screen_w), _to_px(y, _screen_h))
    time.sleep(_CLICK_DELAY)
    _mouse_click(_LDOWN, _LUP)
    time.sleep(0.08)
    _mouse_click(_LDOWN, _LUP)

def _do_drag(x1: int, y1: int, x2: int, y2: int) -> None:
    _smooth_move(_to_px(x1, _screen_w), _to_px(y1, _screen_h))
    time.sleep(0.1)
    _user32.mouse_event(_LDOWN, 0, 0, 0, 0)
    time.sleep(0.1)
    _smooth_move(_to_px(x2, _screen_w), _to_px(y2, _screen_h))
    time.sleep(0.1)
    _user32.mouse_event(_LUP, 0, 0, 0, 0)

def _parse_actions(raw: str) -> list[str]:
    out: list[str] = []
    section = ""
    for line in raw.splitlines():
        s = line.strip()
        u = s.upper().rstrip(":")
        if u == "NARRATIVE":
            section = "narrative"
            continue
        if u == "ACTIONS":
            section = "actions"
            continue
        if section == "actions" and s:
            out.append(s)
    return out

def _parse_call(line: str) -> tuple[str, list[object]] | None:
    p = line.find("(")
    if p == -1:
        return None
    name = line[:p].strip()
    if name not in KNOWN_FUNCTIONS:
        return None
    try:
        args = list(ast.literal_eval(f"({line[p + 1 : line.rfind(')')]},)"))
    except (ValueError, SyntaxError):
        return None
    return name, args

def _run_capture(actions: list[str], width: int, height: int, marks: bool, sandbox: bool, sandbox_reset: bool) -> str:
    payload = json.dumps(
        {"actions": actions, "width": width, "height": height, "marks": marks, "sandbox": sandbox, "sandbox_reset": sandbox_reset}
    )
    r = subprocess.run([sys.executable, str(CAPTURE_SCRIPT)], input=payload, capture_output=True, text=True)
    return r.stdout

def main() -> None:
    request = json.loads(sys.stdin.read() or "{}")
    raw = str(request.get("raw", ""))
    tools: dict[str, bool] = request.get("tools", {}) if isinstance(request.get("tools", {}), dict) else {}
    master_execute = bool(request.get("execute", True))
    width = int(request.get("width", 0))
    height = int(request.get("height", 0))
    marks = bool(request.get("marks", True))

    sandbox = bool(request.get("sandbox", SANDBOX_DEFAULT))
    sandbox_reset = bool(request.get("sandbox_reset", False))

    physical_execute = bool(request.get("physical_execution", PHYSICAL_EXECUTION_DEFAULT))
    if sandbox:
        physical_execute = False

    executed: list[str] = []
    noted: list[str] = []
    wants_screenshot = False

    for line in _parse_actions(raw):
        parsed = _parse_call(line)
        if parsed is None:
            continue
        name, args = parsed

        if name == "screenshot":
            wants_screenshot = True
            noted.append(line)
            continue
        if name == "focus":
            noted.append(line)
            continue
        if not master_execute or not tools.get(name, True):
            noted.append(line)
            continue

        try:
            match name:
                case "left_click" if len(args) >= 2:
                    if physical_execute:
                        _do_left_click(int(args[0]), int(args[1]))
                    executed.append(line)
                case "right_click" if len(args) >= 2:
                    if physical_execute:
                        _do_right_click(int(args[0]), int(args[1]))
                    executed.append(line)
                case "double_left_click" if len(args) >= 2:
                    if physical_execute:
                        _do_double_left_click(int(args[0]), int(args[1]))
                    executed.append(line)
                case "drag" if len(args) >= 4:
                    if physical_execute:
                        _do_drag(int(args[0]), int(args[1]), int(args[2]), int(args[3]))
                    executed.append(line)
                case "type" if args:
                    if physical_execute:
                        _type_text(str(args[0]))
                    executed.append(line)
                case _:
                    noted.append(line)
        except Exception:
            noted.append(line)

    screenshot_b64 = _run_capture(executed, width, height, marks, sandbox, sandbox_reset)
    sys.stdout.write(
        json.dumps(
            {
                "executed": executed,
                "noted": noted,
                "wants_screenshot": wants_screenshot,
                "screenshot_b64": screenshot_b64,
            }
        )
    )
    sys.stdout.flush()

if __name__ == "__main__":
    main()
