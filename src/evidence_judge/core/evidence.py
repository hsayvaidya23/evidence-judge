from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Channel(StrEnum):
    ACTIONS = "actions"
    NETWORK = "network"
    SNAPSHOTS = "snapshots"
    SCREENSHOTS = "screenshots"
    CONSOLE = "console"
    FINAL_STATE = "final_state"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ActionEvent(_Frozen):
    id: str
    ts: float = Field(description="Milliseconds since run start")
    name: str = Field(
        description="Normalised action name: click, fill, goto, press, select, check, ..."
    )
    selector: str | None = None
    value: str | None = None
    url: str | None = Field(default=None, description="Page URL when the action ran")
    error: str | None = None
    call_id: str | None = None


class NetworkEvent(_Frozen):
    id: str
    ts: float
    method: str
    url: str
    status: int | None = None
    resource_type: str | None = None
    request_headers: dict[str, str] = Field(default_factory=dict[str, str])
    request_body: str | None = None
    response_headers: dict[str, str] = Field(default_factory=dict[str, str])
    response_body: str | None = None
    mime_type: str | None = None
    failed: bool = False

    @property
    def is_mutation(self) -> bool:
        return self.method.upper() in {"POST", "PUT", "PATCH", "DELETE"}

    @property
    def ok(self) -> bool:
        return self.status is not None and 200 <= self.status < 400 and not self.failed


class PageSnapshot(_Frozen):
    id: str
    ts: float
    url: str
    title: str | None = None
    text: str = Field(default="", description="Visible text, whitespace-normalised")
    html: str | None = None
    call_id: str | None = None


class Screenshot(_Frozen):
    id: str
    ts: float
    mime_type: str = "image/jpeg"
    width: int | None = None
    height: int | None = None
    resource: str = Field(description="Key into Run.blobs")


class ConsoleEvent(_Frozen):
    id: str
    ts: float
    level: str
    text: str


class PageState(_Frozen):
    url: str
    title: str | None = None
    text: str = ""
    snapshot_id: str | None = None
    screenshot_id: str | None = None


class Run(BaseModel):
    """Everything recorded about one agent run, normalised from any source."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    source: str = Field(description="playwright-trace, har, browser-use, ...")
    task: str | None = None
    agent_answer: str | None = None
    agent_reasoning: list[str] = Field(default_factory=list[str])
    actions: list[ActionEvent] = Field(default_factory=list[ActionEvent])
    network: list[NetworkEvent] = Field(default_factory=list[NetworkEvent])
    snapshots: list[PageSnapshot] = Field(default_factory=list[PageSnapshot])
    screenshots: list[Screenshot] = Field(default_factory=list[Screenshot])
    console: list[ConsoleEvent] = Field(default_factory=list[ConsoleEvent])
    initial_state: PageState | None = None
    final_state: PageState | None = None
    captured: set[Channel] = Field(
        default_factory=set[Channel],
        description="Channels that were recorded. Absence of evidence only counts on captured channels.",
    )
    blobs: dict[str, bytes] = Field(default_factory=dict[str, bytes], exclude=True, repr=False)
    metadata: dict[str, Any] = Field(default_factory=dict[str, Any])

    def merge(self, other: Run) -> Run:
        """Combine evidence from another source describing the same run (e.g. trace + HAR)."""
        seen = {(n.method, n.url, n.ts) for n in self.network}
        extra_net = [n for n in other.network if (n.method, n.url, n.ts) not in seen]
        return self.model_copy(
            update={
                "actions": self.actions or other.actions,
                "network": [*self.network, *extra_net],
                "snapshots": self.snapshots or other.snapshots,
                "screenshots": self.screenshots or other.screenshots,
                "console": self.console or other.console,
                "initial_state": self.initial_state or other.initial_state,
                "final_state": self.final_state or other.final_state,
                "captured": self.captured | other.captured,
                "blobs": {**other.blobs, **self.blobs},
                "agent_answer": self.agent_answer or other.agent_answer,
                "agent_reasoning": self.agent_reasoning or other.agent_reasoning,
                "task": self.task or other.task,
            }
        )
