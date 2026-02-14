"""
FILENAME: main.py
PIPELINE: main.py -> execute.py -> capture.py -> execute.py -> main.py

RULES (THIS FILE):
- Standalone executable. Never imported by other FRANZ scripts.
- Stdlib-only. Windows 11. Python 3.13+.
- stdout must contain ONLY the raw VLM response text.
- Single source of truth state: state.story is the prior raw VLM output verbatim.
- Subprocess I/O is JSON over stdin/stdout pipes only.
- Focus/cropping is removed. All coordinates are always in full-screen 0-1000 space.

PURPOSE:
- Drive the closed loop:
  1) Send the prior raw VLM output to execute.py for optional input simulation and screenshot capture.
  2) Send (state.story + screenshot) to the VLM endpoint using a fixed system prompt.
  3) Print the VLM raw response exactly and store it verbatim as the next state.story.

WHAT WORKS:
- Multi-turn narrative recursion: the VLM sees its own prior output verbatim each turn.
- Action marks correspond to actions that were actually executed by execute.py.

NEEDS FIX / REFACTOR:
- Network/API errors currently terminate the loop; add a retry strategy without printing to stdout.
- Config is hard-coded; consolidate into a single config record if it grows.
"""
import base64
import importlib
import json
import subprocess
import sys
import time
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final

import config as franz_config

API: Final[str] = "http://localhost:1234/v1/chat/completions"
MODEL: Final[str] = "qwen3-vl-2b-instruct-1m"

WIDTH: Final[int] = 512
HEIGHT: Final[int] = 288
VISUAL_MARKS: Final[bool] = True
LOOP_DELAY: Final[float] = 1.0
EXECUTE_ACTIONS: Final[bool] = True

SANDBOX: Final[bool] = True
SANDBOX_RESET: Final[bool] = False
PHYSICAL_EXECUTION: Final[bool] = False
DEBUG_DUMP: Final[bool] = True

EXECUTE_SCRIPT: Final[Path] = Path(__file__).parent / "execute.py"

SANDBOX_CANVAS: Final[Path] = Path(__file__).parent / "sandbox_canvas.bmp"

SYSTEM_PROMPT: Final[str] = """
You control a Windows 11 desktop using these functions:
left_click(x,y), right_click(x,y), double_left_click(x,y), drag(x1,y1,x2,y2), type(text), screenshot().
Coordinates are integers in 0..1000 relative to the current screenshot (0,0 top-left; 1000,1000 bottom-right).
Marks on the screenshot show actions that were actually executed.

Reply in exactly two sections:

NARRATIVE:
Briefly describe what you will do next and ask any needed questions. No coordinates here.

ACTIONS:
One function call per line. No extra text. Use screenshot() whenever you need a fresh view.
If you have nothing else to do, output screenshot().

ULTIMATE GOAL: drawing a cat sketch using mouse drag actions.
""".strip()


@dataclass(slots=True)
class ToolConfig:
    left_click: bool = True
    right_click: bool = True
    double_left_click: bool = True
    drag: bool = True
    type: bool = True
    screenshot: bool = True

    def to_dict(self) -> dict[str, bool]:
        return {
            "left_click": self.left_click,
            "right_click": self.right_click,
            "double_left_click": self.double_left_click,
            "drag": self.drag,
            "type": self.type,
            "screenshot": self.screenshot,
        }


TOOLS: Final[ToolConfig] = ToolConfig()


@dataclass(slots=True)
class PipelineState:
    story: str = ""
    turn: int = 0


def _sampling_dict() -> dict[str, float | int]:
    return {
        "temperature": float(franz_config.TEMPERATURE),
        "top_p": float(franz_config.TOP_P),
        "max_tokens": int(franz_config.MAX_TOKENS),
    }


def _infer(screenshot_b64: str, story: str) -> str:
    payload: dict[str, object] = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": story},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{screenshot_b64}"}},
                ],
            },
        ],
        **_sampling_dict(),
    }
    req = urllib.request.Request(API, json.dumps(payload).encode(), {"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as resp:
        body: dict[str, object] = json.load(resp)
    return body["choices"][0]["message"]["content"]  # type: ignore[index,return-value]


def _run_executor(raw: str) -> dict[str, object]:
    executor_input = json.dumps(
        {
            "raw": raw,
            "tools": TOOLS.to_dict(),
            "execute": EXECUTE_ACTIONS,
            "physical_execution": PHYSICAL_EXECUTION,
            "sandbox": SANDBOX,
            "sandbox_reset": SANDBOX_RESET,
            "width": WIDTH,
            "height": HEIGHT,
            "marks": VISUAL_MARKS,
        }
    )
    result = subprocess.run([sys.executable, str(EXECUTE_SCRIPT)], input=executor_input, capture_output=True, text=True)
    return json.loads(result.stdout or "{}")


def _load_injected(paths: list[Path]) -> Iterator[str]:
    for path in paths:
        data: dict[str, object] = json.loads(path.read_text(encoding="utf-8"))
        yield data["choices"][0]["message"]["content"]  # type: ignore[index,return-value]


def _dump(dump_dir: Path, state: PipelineState, raw: str, executor_result: dict[str, object]) -> None:
    ts = int(time.time() * 1000)
    screenshot_b64 = str(executor_result.get("screenshot_b64", ""))
    if screenshot_b64:
        (dump_dir / f"{ts}.png").write_bytes(base64.b64decode(screenshot_b64))
    run_state = {
        "turn": state.turn,
        "story": state.story,
        "vlm_raw": raw,
        "executed": executor_result.get("executed", []),
        "noted": executor_result.get("noted", []),
        "wants_screenshot": executor_result.get("wants_screenshot", False),
        "execute_actions": EXECUTE_ACTIONS,
        "tools": TOOLS.to_dict(),
        "timestamp": datetime.now().isoformat(),
    }
    (dump_dir / "state.json").write_text(json.dumps(run_state, indent=2), encoding="utf-8")


def main() -> None:
    injected_paths = [Path(arg) for arg in sys.argv[1:]]
    injected_responses: Iterator[str] | None = None
    if injected_paths:
        for path in injected_paths:
            if not path.is_file():
                sys.exit(1)
        injected_responses = _load_injected(injected_paths)

    dump_dir: Path | None = None
    if DEBUG_DUMP:
        dump_dir = Path("dump") / datetime.now().strftime("run_%Y%m%d_%H%M%S")
        dump_dir.mkdir(parents=True, exist_ok=True)

    if SANDBOX and not SANDBOX_CANVAS.is_file():
        _run_executor("")

    time.sleep(3)

    state = PipelineState()

    while True:
        state.turn += 1
        importlib.reload(franz_config)
        executor_result = _run_executor(state.story)
        screenshot_b64 = str(executor_result.get("screenshot_b64", ""))

        raw: str | None = None
        if injected_responses is not None:
            raw = next(injected_responses, None)
            if raw is None:
                break
        if raw is None:
            raw = _infer(screenshot_b64, state.story)

        sys.stdout.write(raw)
        sys.stdout.flush()

        state.story = raw

        if dump_dir is not None:
            _dump(dump_dir, state, raw, executor_result)

        time.sleep(LOOP_DELAY)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        sys.exit(1)
