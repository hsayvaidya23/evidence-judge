from __future__ import annotations


class IngestError(Exception):
    """A recorded run could not be loaded."""


class TraceFormatError(IngestError):
    """A Playwright trace is malformed, truncated or missing required entries."""


class UnsupportedTraceVersionError(TraceFormatError):
    """A Playwright trace uses a format version this parser does not understand."""

    def __init__(self, version: object, supported: frozenset[int]) -> None:
        self.version = version
        self.supported = supported
        versions = ", ".join(str(v) for v in sorted(supported))
        super().__init__(
            f"unsupported Playwright trace version {version!r} (supported: {versions}); "
            "re-record the trace with a supported Playwright release"
        )


class HarFormatError(IngestError):
    """A HAR file is not valid HAR 1.2 JSON."""


class BrowserUseFormatError(IngestError):
    """A browser-use history file does not match the AgentHistoryList schema."""
