from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from enum import StrEnum
from typing import Annotated, Any, Final, Self, cast
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, PlainValidator

BlobLoader = Callable[[], bytes | None]
"""Reads one blob on demand; returns None when it can no longer be read."""


class BlobStore(Mapping[str, bytes]):
    """Screenshot and resource bytes keyed by ``Screenshot.resource``.

    Values are either held in memory or read on demand through a loader, so a long recording
    does not keep every frame resident. A loader that fails makes the key behave as missing
    on lookup (``get`` returns None). The store is immutable; copies share it.
    """

    __slots__ = ("_data", "_loaders")

    def __init__(
        self,
        data: Mapping[str, bytes] | None = None,
        *,
        loaders: Mapping[str, BlobLoader] | None = None,
    ) -> None:
        self._data: dict[str, bytes] = dict(data or {})
        self._loaders: dict[str, BlobLoader] = {
            k: v for k, v in (loaders or {}).items() if k not in self._data
        }

    @classmethod
    def of(cls, blobs: Mapping[str, bytes]) -> BlobStore:
        return blobs if isinstance(blobs, BlobStore) else cls(blobs)

    def __getitem__(self, key: str) -> bytes:
        if key in self._data:
            return self._data[key]
        value = self._loaders[key]()
        if value is None:
            raise KeyError(key)
        return value

    def __iter__(self) -> Iterator[str]:
        yield from self._data
        yield from self._loaders

    def __len__(self) -> int:
        return len(self._data) + len(self._loaders)

    def __contains__(self, key: object) -> bool:
        return key in self._data or key in self._loaders

    def __repr__(self) -> str:
        return f"BlobStore({len(self._data)} loaded, {len(self._loaders)} lazy)"

    def __copy__(self) -> Self:
        return self

    def __deepcopy__(self, memo: dict[int, Any]) -> Self:
        return self

    def union(self, other: Mapping[str, bytes]) -> BlobStore:
        """Both stores' blobs; on a key collision this store's blob wins."""
        theirs = BlobStore.of(other)
        data = {k: v for k, v in theirs._data.items() if k not in self}
        loaders = {k: v for k, v in theirs._loaders.items() if k not in self}
        return BlobStore({**data, **self._data}, loaders={**loaders, **self._loaders})


def _as_blob_store(value: object) -> BlobStore:
    if isinstance(value, BlobStore):
        return value
    if not isinstance(value, Mapping):
        raise ValueError("blobs must map resource names to bytes")
    out: dict[str, bytes] = {}
    for key, blob in cast(Mapping[object, object], value).items():
        if not isinstance(key, str) or not isinstance(blob, bytes):
            raise ValueError("blobs must map resource names to bytes")
        out[key] = blob
    return BlobStore(out)


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


WEB_SCHEMES: Final = frozenset({"http", "https"})


def is_web_page_url(url: str) -> bool:
    """Whether `url` is a page a site served: http or https, never about:, data:, view-source:,
    file:, browser-internal or extension pages, which an agent can fill with its own text."""
    try:
        return urlsplit(url).scheme.lower() in WEB_SCHEMES
    except ValueError:
        return False


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
    blobs: Annotated[Mapping[str, bytes], PlainValidator(_as_blob_store)] = Field(
        default_factory=BlobStore,
        exclude=True,
        repr=False,
        description="Screenshot and resource bytes; a BlobStore that may read them lazily",
    )
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
                "blobs": BlobStore.of(self.blobs).union(other.blobs),
                "agent_answer": self.agent_answer or other.agent_answer,
                "agent_reasoning": self.agent_reasoning or other.agent_reasoning,
                "task": self.task or other.task,
            }
        )
