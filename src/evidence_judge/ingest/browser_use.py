"""Load a browser-use ``AgentHistoryList`` JSON (``history.save_to_file``) into a Run.

Each history item is one agent step: ``model_output`` (thinking, evaluation_previous_goal,
memory, next_goal and the ``action`` list), ``result`` (one ActionResult per executed action)
and ``state`` (url, title, interacted elements and screenshot captured before the step).
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from evidence_judge.core.actions import OTHER, known_action
from evidence_judge.core.evidence import (
    ActionEvent,
    Channel,
    PageState,
    Run,
    Screenshot,
    is_web_page_url,
)
from evidence_judge.ingest._json import (
    JsonObject,
    as_float,
    as_list,
    as_object,
    as_str,
    read_capped,
)
from evidence_judge.ingest.errors import BrowserUseFormatError

SOURCE: Final = "browser-use"
HISTORY_FILE: Final = "history.json"

_VALUE_KEYS: Final = ("url", "text", "keys", "query", "path")
_DONE = "done"
_REASONING_FIELDS: Final = (
    ("thinking", "Thinking"),
    ("evaluation_previous_goal", "Evaluation"),
    ("memory", "Memory"),
    ("next_goal", "Next goal"),
)
_SYNTHETIC_STEP_MS = 1000.0
_IMAGE_KINDS: Final = "a PNG, JPEG, WebP or GIF image"

MAX_SCREENSHOT_BYTES: Final = 10 * 1024 * 1024
"""Default cap on one screenshot; larger ones are skipped with a warning."""
MAX_HISTORY_BYTES: Final = 50 * 1024 * 1024
"""Default cap on the history file's size; larger files fail with ``TooLargeError``."""


def load_browser_use_history(
    path: str | Path,
    *,
    run_id: str | None = None,
    screenshot_root: str | Path | None = None,
    max_screenshot_bytes: int = MAX_SCREENSHOT_BYTES,
    max_file_bytes: int = MAX_HISTORY_BYTES,
) -> Run:
    """Parse a browser-use history file, or the ``history.json`` in a run directory.

    Only actions that produced an ActionResult are recorded: browser-use skips the rest of a
    step's actions once the page changes. Screenshots are loaded from ``state.screenshot_path``
    (relative paths resolve against the history file) or an inline base64 ``state.screenshot``.

    The history is untrusted, so a screenshot path is only read when it resolves (symlinks
    included) inside ``screenshot_root`` (default: the history file's directory), is a regular
    file of at most ``max_screenshot_bytes`` and starts with PNG, JPEG, WebP or GIF magic bytes.
    Anything else is skipped with a warning in ``metadata["warnings"]``.

    Raises:
        BrowserUseFormatError: the file is not JSON or has no ``history`` list.
        TooLargeError: the file is larger than ``max_file_bytes``.
    """
    history_path = Path(path)
    if history_path.is_dir():
        history_path = history_path / HISTORY_FILE
        if not history_path.is_file():
            raise BrowserUseFormatError(f"{path}: run directory has no {HISTORY_FILE}")
    steps = _read_history(history_path, max_file_bytes)
    root = Path(screenshot_root) if screenshot_root is not None else history_path.parent
    builder = _HistoryBuilder(root.resolve(), max_screenshot_bytes)
    origin = _step_start(steps[0]) if steps else None
    for number, step in enumerate(steps, 1):
        start = _step_start(step)
        if origin is None or start is None:
            builder.synthetic_time = True
            ts = (number - 1) * _SYNTHETIC_STEP_MS
        else:
            ts = round((start - origin) * 1000, 3)
        builder.add_step(number, ts, step)
    return builder.build(run_id or history_path.stem)


def _read_history(path: Path, max_bytes: int) -> list[JsonObject]:
    raw = read_capped(path, max_bytes)
    try:
        data = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise BrowserUseFormatError(f"{path}: not valid JSON ({exc})") from exc
    except UnicodeDecodeError as exc:
        raise BrowserUseFormatError(f"{path}: not UTF-8 text") from exc
    except RecursionError as exc:
        raise BrowserUseFormatError(f"{path}: JSON nests too deeply") from exc
    history = as_object(data).get("history")
    steps = as_list(history)
    if not steps and history != []:
        raise BrowserUseFormatError(f"{path}: missing 'history' list; not an AgentHistoryList")
    return [as_object(step) for step in steps]


def _step_start(step: JsonObject) -> float | None:
    return as_float(as_object(step.get("metadata")).get("step_start_time"))


@dataclass(slots=True)
class _HistoryBuilder:
    root: Path
    max_screenshot_bytes: int
    synthetic_time: bool = False
    actions: list[ActionEvent] = field(default_factory=list[ActionEvent])
    reasoning: list[str] = field(default_factory=list[str])
    screenshots: list[Screenshot] = field(default_factory=list[Screenshot])
    blobs: dict[str, bytes] = field(default_factory=dict[str, bytes])
    answer: str | None = None
    success: bool | None = None
    last_state: JsonObject = field(default_factory=JsonObject)
    last_state_screenshot: str | None = None
    skipped_actions: int = 0
    warnings: list[str] = field(default_factory=list[str])

    def add_step(self, number: int, ts: float, step: JsonObject) -> None:
        state = as_object(step.get("state"))
        output = as_object(step.get("model_output"))
        results = [as_object(r) for r in as_list(step.get("result"))]
        shot_id = self._add_screenshot(number, ts, state)
        self._add_reasoning(number, output)
        planned = [_single_action(a) for a in as_list(output.get("action"))]
        planned = [a for a in planned if a is not None]
        self.skipped_actions += max(0, len(planned) - len(results))
        elements = as_list(state.get("interacted_element"))
        done_params: JsonObject = {}
        for index, ((name, params), result) in enumerate(zip(planned, results, strict=False)):
            if name == _DONE:
                done_params = params
                continue
            element = as_object(elements[index]) if index < len(elements) else {}
            self._add_action(name, params, result, element, ts, state)
        for result in results:
            if result.get("is_done") is True:
                self._set_answer(done_params, result)
        if is_web_page_url(as_str(state.get("url")) or ""):
            self.last_state = state
            self.last_state_screenshot = shot_id

    def _add_action(
        self,
        name: str,
        params: JsonObject,
        result: JsonObject,
        element: JsonObject,
        ts: float,
        state: JsonObject,
    ) -> None:
        self.actions.append(
            ActionEvent(
                id=f"act#{len(self.actions) + 1}",
                ts=ts,
                name=known_action(name) or OTHER,
                selector=_selector(element, params),
                value=next((v for k in _VALUE_KEYS if (v := as_str(params.get(k)))), None),
                url=as_str(state.get("url")),
                error=as_str(result.get("error")),
            )
        )

    def _set_answer(self, params: JsonObject, result: JsonObject) -> None:
        answer = as_str(result.get("extracted_content")) or as_str(params.get("text"))
        if answer:
            self.answer = answer
        success = result.get("success", params.get("success"))
        if isinstance(success, bool):
            self.success = success

    def _add_reasoning(self, number: int, output: JsonObject) -> None:
        brain = {**as_object(output.get("current_state")), **output}
        lines = [
            f"{label}: {text}"
            for key, label in _REASONING_FIELDS
            if (text := (as_str(brain.get(key)) or "").strip())
        ]
        if lines:
            self.reasoning.append(f"Step {number}\n" + "\n".join(lines))

    def _add_screenshot(self, number: int, ts: float, state: JsonObject) -> str | None:
        image = self._screenshot_bytes(state)
        if image is None:
            return None
        kind = _image_type(image)
        if kind is None:
            self.warnings.append(f"step {number}: ignored a screenshot that is not {_IMAGE_KINDS}")
            return None
        mime, suffix = kind
        resource = f"browser-use/step-{number}{suffix}"
        self.blobs[resource] = image
        shot = Screenshot(
            id=f"shot#{len(self.screenshots) + 1}", ts=ts, mime_type=mime, resource=resource
        )
        self.screenshots.append(shot)
        return shot.id

    def _screenshot_bytes(self, state: JsonObject) -> bytes | None:
        inline = as_str(state.get("screenshot"))
        if inline:
            return self._inline_screenshot(inline)
        name = as_str(state.get("screenshot_path"))
        if not name:
            return None
        return self._file_screenshot(name)

    def _inline_screenshot(self, encoded: str) -> bytes | None:
        if len(encoded) > (self.max_screenshot_bytes + 2) // 3 * 4:
            self.warnings.append(self._too_large("an inline screenshot"))
            return None
        try:
            return base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            self.warnings.append("ignored an inline screenshot that is not base64")
            return None

    def _file_screenshot(self, name: str) -> bytes | None:
        try:
            file = (self.root / name).resolve()
        except (OSError, RuntimeError, ValueError):
            self.warnings.append(f"ignored an unresolvable screenshot path: {name}")
            return None
        if not file.is_relative_to(self.root):
            self.warnings.append(f"ignored a screenshot path outside the screenshot root: {name}")
            return None
        if not file.exists():
            self.warnings.append(f"screenshot not found: {name}")
            return None
        if not file.is_file():
            self.warnings.append(f"ignored a screenshot that is not a regular file: {name}")
            return None
        try:
            with file.open("rb") as handle:
                if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                    self.warnings.append(f"ignored a screenshot that is not a regular file: {name}")
                    return None
                data = handle.read(self.max_screenshot_bytes + 1)
        except OSError:
            self.warnings.append(f"screenshot not readable: {name}")
            return None
        if len(data) > self.max_screenshot_bytes:
            self.warnings.append(self._too_large(f"screenshot {name}"))
            return None
        return data

    def _too_large(self, what: str) -> str:
        return f"ignored {what} larger than {self.max_screenshot_bytes} bytes"

    def build(self, run_id: str) -> Run:
        captured = {Channel.ACTIONS}
        if self.screenshots:
            captured.add(Channel.SCREENSHOTS)
        metadata: JsonObject = {"agent_reported_success": self.success}
        if self.skipped_actions:
            metadata["skipped_actions"] = self.skipped_actions
        if self.synthetic_time:
            metadata["synthetic_timestamps"] = True
        if self.warnings:
            metadata["warnings"] = list(self.warnings)
        return Run(
            run_id=run_id,
            source=SOURCE,
            agent_answer=self.answer,
            agent_reasoning=self.reasoning,
            actions=self.actions,
            screenshots=self.screenshots,
            final_state=self._final_state(),
            captured=captured,
            blobs=self.blobs,
            metadata=metadata,
        )

    def _final_state(self) -> PageState | None:
        url = as_str(self.last_state.get("url"))
        if not url:
            return None
        return PageState(
            url=url,
            title=as_str(self.last_state.get("title")),
            screenshot_id=self.last_state_screenshot,
        )


def _single_action(raw: object) -> tuple[str, JsonObject] | None:
    """An ActionModel dump is ``{action_name: params}`` with unset actions omitted or null."""
    present = [(name, params) for name, params in as_object(raw).items() if params is not None]
    if not present:
        return None
    name, params = present[0]
    return name, as_object(params)


def _selector(element: JsonObject, params: JsonObject) -> str | None:
    element_id = as_str(as_object(element.get("attributes")).get("id"))
    if element_id:
        return f"#{element_id}"
    xpath = as_str(element.get("x_path"))
    if xpath:
        return f"xpath={xpath}"
    index = params.get("index")
    return f"index={index}" if isinstance(index, int) and not isinstance(index, bool) else None


def _image_type(image: bytes) -> tuple[str, str] | None:
    if image.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", ".png"
    if image.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", ".jpg"
    if image.startswith(b"RIFF") and image[8:12] == b"WEBP":
        return "image/webp", ".webp"
    if image.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif", ".gif"
    return None
