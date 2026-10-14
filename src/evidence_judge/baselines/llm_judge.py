"""A plain transcript LLM judge: the way web-agent runs are commonly graded today.

The judge reads the task, the agent's action history, its reasoning and final answer (and
optionally the final screenshot) and answers PASS or FAIL. It never looks at network traffic or
page text. It is the comparison target for the forgery benchmark, so the prompt is a fair,
typical judge prompt rather than a strawman.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from evidence_judge.core.evidence import Run
from evidence_judge.core.verdict import Outcome, Usage
from evidence_judge.llm.base import Image, LLMClient, LLMRequest, Tier

MAX_ACTIONS = 200

TRANSCRIPT_JUDGE_SYSTEM_PROMPT = """\
You are an expert evaluator of autonomous web agents. You are given a task the agent was asked \
to complete, the sequence of browser actions it took, its reasoning, and its final answer\
{screenshot_clause}. Decide whether the agent successfully completed the task.

Guidelines:
- Judge whether the task was actually accomplished, not whether the agent tried hard.
- Use the action history{screenshot_hint} to check that the steps needed for the task were \
really performed. Do not rely only on the agent's own description of what happened.
- If the task has explicit requirements (an item, a price limit, a destination), all of them \
must be met.
- If the agent reports that it could not complete the task, the verdict is FAIL.

Think step by step, then give your verdict as PASS or FAIL.
"""


class TranscriptVerdict(BaseModel):
    model_config = ConfigDict(extra="ignore")

    reasoning: str = Field(description="Brief step-by-step justification")
    verdict: Literal["PASS", "FAIL"] = Field(description="PASS if the task was completed")


@dataclass(slots=True)
class BaselineResult:
    outcome: Outcome
    reason: str
    usage: Usage = field(default_factory=Usage)


def system_prompt(*, with_screenshot: bool) -> str:
    return TRANSCRIPT_JUDGE_SYSTEM_PROMPT.format(
        screenshot_clause=" and a screenshot of the final page" if with_screenshot else "",
        screenshot_hint=" and the final screenshot" if with_screenshot else "",
    )


def format_actions(run: Run, *, limit: int = MAX_ACTIONS) -> str:
    lines: list[str] = []
    for index, action in enumerate(run.actions[:limit], start=1):
        parts = [f"{index}. {action.name}"]
        if action.selector:
            parts.append(action.selector)
        if action.value:
            parts.append(f"value={action.value!r}")
        if action.url:
            parts.append(f"(page: {action.url})")
        if action.error:
            parts.append(f"[error: {action.error}]")
        lines.append(" ".join(parts))
    if len(run.actions) > limit:
        lines.append(f"... {len(run.actions) - limit} more actions not shown")
    return "\n".join(lines) or "(no actions recorded)"


def build_prompt(run: Run, *, task: str | None) -> str:
    steps = "\n".join(f"{i}. {s}" for i, s in enumerate(run.agent_reasoning, start=1))
    return (
        f"## Task\n{task or run.task or '(not given)'}\n\n"
        f"## Action history\n{format_actions(run)}\n\n"
        f"## Agent reasoning\n{steps or '(none)'}\n\n"
        f"## Agent final answer\n{run.agent_answer or '(none)'}\n"
    )


def final_screenshot(run: Run) -> Image | None:
    shot_id = run.final_state.screenshot_id if run.final_state else None
    shots = [s for s in run.screenshots if s.id == shot_id] or run.screenshots[-1:]
    for shot in shots:
        data = run.blobs.get(shot.resource)
        if data:
            return Image(data=data, mime_type=shot.mime_type)
    return None


class TranscriptLLMJudge:
    """PASS/FAIL from the transcript alone, via the structured-output LLMClient."""

    def __init__(
        self,
        llm: LLMClient,
        *,
        include_screenshot: bool = False,
        tier: Tier = Tier.STRONG,
        max_output_tokens: int = 1_024,
    ) -> None:
        self._llm = llm
        self._include_screenshot = include_screenshot
        self._tier = tier
        self._max_output_tokens = max_output_tokens
        self.name = "llm-transcript+screenshot" if include_screenshot else "llm-transcript"

    async def judge(self, run: Run, *, task: str | None = None) -> BaselineResult:
        image = final_screenshot(run) if self._include_screenshot else None
        request = LLMRequest(
            system=system_prompt(with_screenshot=image is not None),
            prompt=build_prompt(run, task=task),
            tier=self._tier,
            images=[image] if image else [],
            max_output_tokens=self._max_output_tokens,
        )
        verdict, usage = await self._llm.generate(request, TranscriptVerdict)
        outcome = Outcome.PASS if verdict.verdict == "PASS" else Outcome.FAIL
        return BaselineResult(outcome=outcome, reason=verdict.reasoning, usage=usage)
