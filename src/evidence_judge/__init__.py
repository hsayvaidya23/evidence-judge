"""Grade browser-agent runs and coding-agent sessions by checking every claim against recorded
evidence.

The names in `__all__` are the supported Python API; everything under submodules is
internal and may change between 0.x releases.
"""

from evidence_judge._version import __version__
from evidence_judge.config import Settings, read_settings
from evidence_judge.core.taskspec import TaskSpec
from evidence_judge.core.verdict import Outcome, RunVerdict
from evidence_judge.engine import Judge, JudgeOptions
from evidence_judge.ingest import load_run
from evidence_judge.llm import build_llm
from evidence_judge.redact import Redactor
from evidence_judge.sessions.ingest import load_session
from evidence_judge.sessions.judge import SessionJudge, SessionJudgeOptions
from evidence_judge.sessions.predicates import SessionSpec

__all__ = [
    "Judge",
    "JudgeOptions",
    "Outcome",
    "Redactor",
    "RunVerdict",
    "SessionJudge",
    "SessionJudgeOptions",
    "SessionSpec",
    "Settings",
    "TaskSpec",
    "__version__",
    "build_llm",
    "load_run",
    "load_session",
    "read_settings",
]
