"""Load a browser-use ``AgentHistoryList`` JSON (``history.save_to_file``) into a Run.

Each history item is one agent step: ``model_output`` (thinking, evaluation_previous_goal,
memory, next_goal and the ``action`` list), ``result`` (one ActionResult per executed action)
and ``state`` (url, title, interacted elements and screenshot captured before the step).
"""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from evidence_judge.core.evidence import ActionEvent, Channel, PageState, Run, Screenshot
from evidence_judge.ingest._json import JsonObject, as_float, as_list, as_object, as_str
from evidence_judge.ingest.errors import BrowserUseFormatError

SOURCE: Final = "browser-use"

_ACTION_NAMES: Final[dict[str, str]] = {
    "navigate": "goto",
    "go_to_url": "goto",
    "open_tab": "goto",
    "click": "click",
    "click_element": "click",
    "click_element_by_index": "click",
    "input": "fill",
    "input_text": "fill",
    "send_keys": "press",
    "select_dropdown": "select",
    "select_dropdown_option": "select",
    "upload_file": "upload",
}
_VALUE_KEYS: Final = ("url", "text", "keys", "query", "path")
_DONE = "done"
_REASONING_FIELDS: Final = (
    ("thinking", "Thinking"),
    ("evaluation_previous_goal", "Evaluation"),
    ("memory", "Memory"),
    ("next_goal", "Next goal"),
)
_SYNTHETIC_STEP_MS = 1000.0


def load_browser_use_history(path: str | Path, *, run_id: str | None = None) -> Run:
    """Parse a browser-use history file.

    Only actions that produced an ActionResult are recorded: browser-use skips the rest of a
    step's actions once the page changes. Screenshots are loaded from ``state.screenshot_path``
    (relative paths resolve against the history file) or an inline base64 ``state.screenshot``.

    Raises:
        BrowserUseFormatError: the file is not JSON or has no ``history`` list.
    """
    history_path = Path(path)
    steps = _read_history(history_path)
    builder = _HistoryBuilder(history_path.parent)
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


def _read_history(path: Path) -> list[JsonObject]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise BrowserUseFormatError(f"{path}: not valid JSON ({exc})") from exc
    except UnicodeDecodeError as exc:
        raise BrowserUseFormatError(f"{path}: not UTF-8 text") from exc
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
        if state:
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
                name=_ACTION_NAMES.get(name, "other"),
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
        mime, suffix = _image_type(image)
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
            try:
                return base64.b64decode(inline, validate=True)
            except (binascii.Error, ValueError):
                self.warnings.append("ignored an inline screenshot that is not base64")
                return None
        name = as_str(state.get("screenshot_path"))
        if not name:
            return None
        file = Path(name) if Path(name).is_absolute() else self.root / name
        try:
            return file.read_bytes()
        except OSError:
            self.warnings.append(f"screenshot not found: {name}")
            return None

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


def _image_type(image: bytes) -> tuple[str, str]:
    if image.startswith(b"\x89PNG"):
        return "image/png", ".png"
    if image.startswith(b"\xff\xd8"):
        return "image/jpeg", ".jpg"
    if image[8:12] == b"WEBP":
        return "image/webp", ".webp"
    return "application/octet-stream", ".bin"
