from __future__ import annotations

import re
from collections.abc import Callable, Iterator, Mapping
from enum import StrEnum
from typing import Annotated, Any, Final, Literal, Self, cast
from urllib.parse import parse_qsl, urlsplit

from pydantic import BaseModel, ConfigDict, Field, PlainValidator

MUTATION_METHODS: Final = frozenset({"POST", "PUT", "PATCH", "DELETE"})
"""HTTP methods that change state: a request with one is a mutation."""
METHOD_OVERRIDE_HEADERS: Final = frozenset(
    {"x-http-method-override", "x-http-method", "x-method-override"}
)
"""Headers a server may read as the request's real method (a GET that acts as a DELETE)."""
_GRAPHQL_TOKEN: Final = re.compile(
    r'"""(?:\\"""|[^"]|"(?!""))*(?:"""|$)|"(?:\\.|[^"\\\n])*"?|#[^\n]*|[_A-Za-z][_0-9A-Za-z]*|\S'
)
_GRAPHQL_OPERATIONS: Final = frozenset({"query", "mutation", "subscription"})


def _graphql_operations(document: str) -> list[tuple[str, str | None]]:
    """The operations a GraphQL document defines, as (kind, name), in order: every top-level
    definition that opens a selection set, read past strings, comments, variables and
    directives (an object literal in a variable default or a directive argument, `($a: I =
    {id: 1})`, `@audit(meta: {by: "x"})`, opens no selection set). Fragments are left out; a
    bare `{ ... }` is an anonymous query."""
    operations: list[tuple[str, str | None]] = []
    header: list[str] = []
    braces = parens = 0
    directive = False
    for part in _GRAPHQL_TOKEN.findall(document):
        if part[0] in '#"' or (parens and part in "{}"):
            continue
        if part == "{":
            if braces == 0 and parens == 0:
                if not header:
                    operations.append(("query", None))
                elif header[0] in _GRAPHQL_OPERATIONS:
                    operations.append((header[0], header[1] if len(header) > 1 else None))
            braces += 1
        elif part == "}":
            braces = max(braces - 1, 0)
            if braces == 0:
                header = []
        elif part in "()" and braces == 0:
            parens = parens + 1 if part == "(" else max(parens - 1, 0)
        elif braces == 0 and parens == 0:
            if part == "@":
                directive = True
            elif part[0].isalpha() or part[0] == "_":
                if not directive:
                    header.append(part)
                directive = False
    return operations


def _graphql_mutation(document: str, operation_names: list[str]) -> bool:
    """The operation the request runs may be a mutation: the one `operationName` names, else
    the only one; with several and none named (or a name that matches none), any mutation.
    Several `operationName` values are ambiguous (servers differ on which one they read): any
    of them naming a mutation counts."""
    operations = _graphql_operations(document)
    if len(set(operation_names)) > 1:
        return any(_graphql_mutation(document, [name]) for name in operation_names)
    wanted = operation_names[0] if operation_names else None
    named = [kind for kind, name in operations if wanted and name == wanted]
    if named:
        return named[0] == "mutation"
    if len(operations) == 1:
        return operations[0][0] == "mutation"
    return any(kind == "mutation" for kind, _ in operations)


def _persisted_only(params: list[tuple[str, str]]) -> bool:
    """A GraphQL persisted query (`extensions={"persistedQuery": ...}`) sent without its
    document: only the server knows whether the hash names a mutation."""
    if any(key == "query" for key, _ in params):
        return False
    return any(key == "extensions" and "persistedQuery" in value for key, value in params)


HarField = Literal[
    "status",
    "resource_type",
    "request_headers",
    "request_body",
    "response_headers",
    "response_body",
    "mime_type",
    "failed",
]
"""A NetworkEvent field a HAR can fill in on a request the trace recorded with it empty."""

_HAR_FIELD_DEFAULTS: Final[dict[HarField, object]] = {
    "status": None,
    "resource_type": None,
    "request_headers": {},
    "request_body": None,
    "response_headers": {},
    "response_body": None,
    "mime_type": None,
    "failed": True,
}

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

    def __deepcopy__(self, _memo: dict[int, Any]) -> Self:
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
    script_sha256: str | None = Field(
        default=None,
        description="For an inject action that ran code the trace recorded in full: the hash "
        "a task spec's trusted_scripts lists to exempt it (core.provenance.script_hash)",
    )
    file_text: str | None = Field(
        default=None,
        description="For an upload: the text of the files it sent, as far as the trace holds "
        "it (the first 64 KB of each text file; binary files are left out)",
    )
    target_text: str | None = Field(
        default=None,
        description="The text of the element the action resolved to, as the recording shows "
        "it (a Playwright trace logs a preview of it, cut short when long)",
    )


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
    synthetic: bool = Field(
        default=False,
        description="The agent's own route handler answered this request (route.fulfill) or "
        "rewrote it (route.continue with overrides): the response, or the request the site "
        "got, is the agent's",
    )
    har_only: bool = Field(
        default=False,
        description="A HAR merged into a trace recorded this request and the trace did not: "
        "only the HAR, a file anyone can write, says it happened",
    )
    har_filled: frozenset[HarField] = Field(
        default_factory=frozenset[HarField],
        description="Fields the trace recorded empty on this request and a HAR merged into it "
        "filled in; the trace never saw their values",
    )

    @property
    def from_har(self) -> bool:
        """Whether a HAR merged into a trace contributed this request or any of its fields."""
        return self.har_only or bool(self.har_filled)

    def as_traced(self) -> NetworkEvent:
        """This request as the trace alone recorded it: the fields a HAR filled in left empty."""
        if not self.har_filled:
            return self
        cleared = {f: _HAR_FIELD_DEFAULTS[f] for f in self.har_filled}
        return self.model_copy(update={**cleared, "har_filled": frozenset[HarField]()})

    @property
    def write_method(self) -> str | None:
        """The state-changing method the request acts as, or None: its own method, one an
        override header (`X-HTTP-Method-Override: DELETE`, any write method in a comma list)
        or a `_method=` query names (`&` or `;` separated), or POST for a GraphQL GET whose
        `query` may run a mutation or that sends only a persisted-query hash, whose operation
        only the server knows."""
        method = self.method.upper()
        if method in MUTATION_METHODS:
            return method
        for name, value in self.request_headers.items():
            if name.lower() not in METHOD_OVERRIDE_HEADERS:
                continue
            listed = (part.strip().upper() for part in value.split(","))
            if named := next((m for m in listed if m in MUTATION_METHODS), None):
                return named
        params = self._query_params
        operations = [v for k, v in params if k == "operationName"]
        for key, value in params:
            if key.lower() == "_method" and value.strip().upper() in MUTATION_METHODS:
                return value.strip().upper()
            if key == "query" and _graphql_mutation(value, operations):
                return "POST"
        if _persisted_only(params):
            return "POST"
        return None

    @property
    def _query_params(self) -> list[tuple[str, str]]:
        return parse_qsl(urlsplit(self.url).query.replace(";", "&"), keep_blank_values=True)

    @property
    def is_mutation(self) -> bool:
        return self.write_method is not None

    @property
    def shown_method(self) -> str:
        """The method as recorded, with the write it acts as when that differs (`GET as
        DELETE`)."""
        method = self.method.upper()
        acts = self.write_method
        if acts is not None and acts != method and _persisted_only(self._query_params):
            return f"{method} (persisted query)"
        return f"{method} as {acts}" if acts is not None and acts != method else method

    @property
    def ok(self) -> bool:
        return self.status is not None and 200 <= self.status < 400 and not self.failed


def trace_witnessed(run: Run) -> Run:
    """`run` with only what its trace recorded: requests only a merged HAR has are dropped and
    fields a HAR filled in are left empty. `run` itself when no HAR contributed anything."""
    if not any(n.from_har for n in run.network):
        return run
    network = [n.as_traced() for n in run.network if not n.har_only]
    return run.model_copy(update={"network": network})


def har_contribution(run: Run) -> tuple[int, int]:
    """How many requests in `run` only a merged HAR recorded, and how many trace requests a HAR
    filled fields in on."""
    only = sum(1 for n in run.network if n.har_only)
    filled = sum(1 for n in run.network if n.har_filled and not n.har_only)
    return only, filled


class PageSnapshot(_Frozen):
    id: str
    ts: float
    url: str
    title: str | None = None
    text: str = Field(default="", description="Visible text, whitespace-normalised")
    unsure_text: str = Field(
        default="",
        description="Text the page may hide, whitespace-normalised: its visibility depends on "
        "styles the reader does not fully model (opacity, colour, clipping, a selector or "
        "condition it cannot evaluate). It never proves the page showed something, and keeps "
        "its absence from proving the page did not",
    )
    opaque: bool = Field(
        default=False,
        description="The page may show text the snapshot does not hold (an empty custom "
        "element may render a closed shadow root, a ::before or ::after rule may give a shown "
        "element text, a shown frame holds a document of its own), so text missing from it is "
        "not proven absent",
    )
    html: str | None = None
    call_id: str | None = None
    field_values: tuple[str, ...] = Field(
        default=(),
        description="Values form fields held (text inputs, text areas, editable elements, "
        "selected options), which are not page text; a snapshot that only changed them adds "
        "them to the last one",
    )


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
    unsure_text: str = Field(default="", description="As PageSnapshot.unsure_text")
    opaque: bool = Field(default=False, description="As PageSnapshot.opaque")
    snapshot_id: str | None = None
    screenshot_id: str | None = None

    @classmethod
    def of(cls, snapshot: PageSnapshot) -> PageState:
        """The page state a snapshot records."""
        return cls(
            url=snapshot.url,
            title=snapshot.title,
            text=snapshot.text,
            unsure_text=snapshot.unsure_text,
            opaque=snapshot.opaque,
            snapshot_id=snapshot.id,
        )


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
