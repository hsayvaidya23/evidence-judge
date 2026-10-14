"""Load recorded agent runs (Playwright traces, HAR logs, browser-use histories) as Runs."""

from evidence_judge.ingest.browser_use import load_browser_use_history
from evidence_judge.ingest.errors import (
    BrowserUseFormatError,
    HarFormatError,
    IngestError,
    TraceFormatError,
    TraceTooLargeError,
    UnsupportedTraceVersionError,
)
from evidence_judge.ingest.har import load_har
from evidence_judge.ingest.loader import attach_har, load_run
from evidence_judge.ingest.playwright_trace import (
    DEFAULT_TRACE_LIMITS,
    SUPPORTED_TRACE_VERSIONS,
    TraceLimits,
    load_playwright_trace,
)

__all__ = [
    "DEFAULT_TRACE_LIMITS",
    "SUPPORTED_TRACE_VERSIONS",
    "BrowserUseFormatError",
    "HarFormatError",
    "IngestError",
    "TraceFormatError",
    "TraceLimits",
    "TraceTooLargeError",
    "UnsupportedTraceVersionError",
    "attach_har",
    "load_browser_use_history",
    "load_har",
    "load_playwright_trace",
    "load_run",
]
