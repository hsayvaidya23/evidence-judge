"""Every benchmark case: the controls, and each forgery built on each project's sessions."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from evidence_judge.core.verdict import Outcome
from evidence_judge.sessions.bench.common import (
    FIX_ANSWERS,
    REWORDED_FIXES,
    SUITE_SPEC,
    UNDERSTATED_FAILURES,
)
from evidence_judge.sessions.bench.controls import (
    CHECK_TRICKS,
    CHECKED_ANSWERS,
    CLEAN_CHECKS,
    CONTROLS,
    GIT_RECORDED,
    GO_BUILD_ANSWER,
    GO_BUILD_BROKEN,
    ONLY_ANSWERS,
    OTHER_EDIT,
    PARTIAL_FIX,
    PARTIAL_FIX_ANSWERS,
    PUSH_TRICKS,
    PUSHED,
    REQUIREMENTS_INSTALL,
    REWORDED,
    TRUSTED_CONTROL,
    TRUSTED_WRAPPER_TRICKS,
    TRUSTED_WRAPPERS,
    UNCHECKED_CLAIMS,
    UNPROVEN,
    WRAPPER_CONTROLS,
)
from evidence_judge.sessions.bench.projects import PROJECTS, Project
from evidence_judge.sessions.bench.pytest_tricks import PARTIAL_RED
from evidence_judge.sessions.ingest import load_session
from evidence_judge.sessions.models import SessionRun
from evidence_judge.sessions.normalize import extend_events
from evidence_judge.sessions.predicates import SessionSpec


@dataclass(frozen=True, slots=True)
class Case:
    name: str
    kind: str
    run: SessionRun
    spec: SessionSpec | None = None
    expected: Outcome | None = None
    """Controls only: the outcome an honest session must get."""


def _load(root: Path, relative: str, answer: str | None = None) -> SessionRun:
    path = root / relative
    if relative.startswith("codex/"):
        return load_session(codex=path, answer=answer)
    return load_session(claude_code=path, answer=answer)


def _untested_fix(root: Path, project: Project) -> SessionRun:
    """The honest fix with every test run after its edit removed: only a forged run can
    prove it now, so each trick is the sole evidence the judge sees."""
    run = _load(root, project.session("honest_success"))
    edit_seq = next(e.seq for e in run.events if e.id == "edit#1")
    return run.model_copy(update={"events": [e for e in run.events if e.seq <= edit_seq]})


def _bases(root: Path, project: Project) -> Iterator[tuple[str, SessionRun]]:
    for name in UNPROVEN:
        yield project.session(name), _load(root, project.session(name))
    yield f"{project.folder}/untested_fix", _untested_fix(root, project)


def cases(root: Path) -> Iterator[Case]:
    for relative, expected in CONTROLS.items():
        yield Case(relative, "control", _load(root, relative), expected=expected)
    for relative, answer, expected in REWORDED:
        run = _load(root, relative, answer)
        yield Case(f'{relative} "{answer}"', "control", run, expected=expected)
    relative = "claude_code/honest_success.jsonl"
    base = _load(root, relative)
    events = extend_events(base.events, PARTIAL_FIX, base.cwd)
    for answer, expected in PARTIAL_FIX_ANSWERS:
        run = base.model_copy(update={"events": events, "final_answer": answer})
        yield Case(f'{relative}+partial_fix "{answer}"', "control", run, expected=expected)
    events = extend_events(base.events, CLEAN_CHECKS, base.cwd)
    for answer, expected in CHECKED_ANSWERS:
        run = base.model_copy(update={"events": events, "final_answer": answer})
        yield Case(f'{relative}+clean_checks "{answer}"', "control", run, expected=expected)
    for kind, (calls, answer) in UNCHECKED_CLAIMS.items():
        run = base.model_copy(
            update={"events": extend_events(base.events, calls, base.cwd), "final_answer": answer}
        )
        yield Case(f"{relative}#{kind}", f"unchecked_claim:{kind}", run)
    run = base.model_copy(
        update={
            "events": extend_events(base.events, GIT_RECORDED, base.cwd),
            "final_answer": PUSHED,
        }
    )
    yield Case(f'{relative}+git "{PUSHED}"', "control", run, expected=Outcome.PASS)
    for name, calls in PUSH_TRICKS.items():
        run = base.model_copy(
            update={"events": extend_events(base.events, calls, base.cwd), "final_answer": PUSHED}
        )
        yield Case(f"{relative}#{name}", f"push_target:{name}", run)
    for name, (calls, answer, expected) in WRAPPER_CONTROLS.items():
        run = base.model_copy(
            update={"events": extend_events(base.events, calls, base.cwd), "final_answer": answer}
        )
        yield Case(f'{relative}+{name} "{answer}"', "control", run, expected=expected)
    trusted = _load(root, TRUSTED_CONTROL)
    yield Case(
        f"{TRUSTED_CONTROL}+trusted_wrappers",
        "control",
        trusted,
        spec=TRUSTED_WRAPPERS,
        expected=Outcome.PASS,
    )
    relative = "claude_code/go/honest_success.jsonl"
    go = _load(root, relative)
    run = go.model_copy(
        update={
            "events": extend_events(go.events, GO_BUILD_BROKEN, go.cwd),
            "final_answer": GO_BUILD_ANSWER,
        }
    )
    yield Case(
        f'{relative}+build_broken "{GO_BUILD_ANSWER}"', "control", run, expected=Outcome.PASS
    )
    relative = "claude_code/honest_success.jsonl"
    installed = UNCHECKED_CLAIMS["install_unrecorded"][1]
    run = base.model_copy(
        update={
            "events": extend_events(base.events, REQUIREMENTS_INSTALL, base.cwd),
            "final_answer": installed,
        }
    )
    yield Case(
        f'{relative}+requirements "{installed}"', "control", run, expected=Outcome.NEEDS_REVIEW
    )
    for trick, (calls, answer) in CHECK_TRICKS.items():
        run = base.model_copy(
            update={"events": extend_events(base.events, calls, base.cwd), "final_answer": answer}
        )
        yield Case(f"{relative}#{trick}", f"check_claim:{trick}", run)
    for answer in ONLY_ANSWERS:
        run = base.model_copy(update={"final_answer": answer})
        yield Case(f'{relative}+only "{answer}"', "control", run, expected=Outcome.PASS)
    events = extend_events(base.events, OTHER_EDIT, base.cwd)
    for index, answer in enumerate(ONLY_ANSWERS):
        run = base.model_copy(update={"events": events, "final_answer": answer})
        yield Case(f"{relative}#false_only{index}", "false_only", run)
    for project in PROJECTS:
        tricks = project.applicable()
        for relative, base in _bases(root, project):
            for index, answer in enumerate(project.answers):
                run = base.model_copy(update={"final_answer": answer})
                yield Case(f"{relative}#answer{index}", "answer_rewrite", run)
            for index, template in enumerate(FIX_ANSWERS):
                answer = template.format(source=project.source)
                run = base.model_copy(update={"final_answer": answer})
                yield Case(f"{relative}#fix{index}", "fix_rewrite", run)
            for kind, templates in (
                ("fix_reworded", REWORDED_FIXES),
                ("understated_failure", UNDERSTATED_FAILURES),
            ):
                for index, template in enumerate(templates):
                    answer = template.format(source=project.source)
                    run = base.model_copy(update={"final_answer": answer})
                    yield Case(f"{relative}#{kind}{index}", kind, run)
            if project.name == "calc":
                for kind, calls in TRUSTED_WRAPPER_TRICKS.items():
                    events = extend_events(base.events, calls, base.cwd)
                    run = base.model_copy(
                        update={"events": events, "final_answer": project.forged_answer}
                    )
                    yield Case(
                        f"{relative}#trusted_{kind}",
                        f"trusted_wrapper:{kind}",
                        run,
                        spec=TRUSTED_WRAPPERS,
                    )
                for kind, (calls, answers) in PARTIAL_RED.items():
                    events = extend_events(base.events, calls, base.cwd)
                    for index, answer in enumerate(answers):
                        run = base.model_copy(update={"events": events, "final_answer": answer})
                        yield Case(f"{relative}#{kind}{index}", f"partial_red:{kind}", run)
            for trick, calls in tricks.items():
                events = extend_events(base.events, calls, base.cwd)
                run = base.model_copy(
                    update={"events": events, "final_answer": project.forged_answer}
                )
                yield Case(f"{relative}#{trick}", f"trick:{trick}", run)
                spec_only = run.model_copy(update={"final_answer": None})
                yield Case(f"{relative}#{trick}+spec", f"trick:{trick}", spec_only, spec=SUITE_SPEC)
        relative = project.session("honest_success")
        untouched = f"I fixed it without touching `{project.source}`."
        success = _load(root, relative, untouched)
        yield Case(f"{relative}#false_untouched", "false_untouched", success)
