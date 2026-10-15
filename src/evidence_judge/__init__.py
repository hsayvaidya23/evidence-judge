"""Grade browser-agent runs by checking every claim against recorded evidence.

The names in `__all__` are the supported Python API; everything under submodules is
internal and may change between 0.x releases.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("evidence-judge")
except PackageNotFoundError:
    __version__ = "0.0.0"

# Bound after __version__, which these modules import from the package.
from evidence_judge.config import Settings
from evidence_judge.core.taskspec import TaskSpec
from evidence_judge.core.verdict import Outcome, RunVerdict
from evidence_judge.engine import Judge, JudgeOptions
from evidence_judge.ingest import load_run
from evidence_judge.llm import build_llm
from evidence_judge.redact import Redactor

__all__ = [
    "Judge",
    "JudgeOptions",
    "Outcome",
    "Redactor",
    "RunVerdict",
    "Settings",
    "TaskSpec",
    "__version__",
    "build_llm",
    "load_run",
]
