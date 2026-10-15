"""The getting-started pages show real CLI output: re-run each example and compare."""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from evidence_judge.cli import app

ROOT = Path(__file__).resolve().parents[2]
PAGES = ROOT / "docs" / "getting-started"
FIRST_JUDGE = PAGES / "first-judge.md"
FORGED = ROOT / "tests" / "fixtures" / "runs" / "forged_success"


@dataclass(frozen=True)
class Example:
    command: str
    output: str
    exit_code: int | None


def _examples(page: Path) -> list[Example]:
    examples: list[Example] = []
    for block in re.findall(r"```console\n(.*?)```", page.read_text(encoding="utf-8"), re.S):
        lines = block.splitlines()
        variables: dict[str, str] = {}
        i = 0
        while i < len(lines):
            line = lines[i]
            i += 1
            if assignment := re.fullmatch(r"\$ (\w+)=(\S+)", line):
                variables[assignment.group(1)] = str(ROOT / assignment.group(2))
            elif line.startswith("$ evidence-judge "):
                command = line[2:]
                while command.endswith("\\"):
                    command = command[:-1] + lines[i]
                    i += 1
                output: list[str] = []
                while i < len(lines) and not lines[i].startswith("$ "):
                    output.append(lines[i])
                    i += 1
                exit_code = None
                if i + 1 < len(lines) and lines[i] == "$ echo $?":
                    exit_code = int(lines[i + 1])
                    i += 2
                for name, value in variables.items():
                    command = command.replace(f"${name}", value)
                examples.append(Example(command, "\n".join(output), exit_code))
    return examples


def _words(text: str) -> str:
    return " ".join(text.split())


def _run(command: str, tmp_path: Path) -> tuple[int, str]:
    args = shlex.split(command)[1:]
    for i, arg in enumerate(args[:-1]):
        if arg in ("--out", "--html"):
            args[i + 1] = str(tmp_path / args[i + 1])
    result = CliRunner().invoke(app, args)
    return result.exit_code, result.output


@pytest.mark.parametrize("example", _examples(FIRST_JUDGE), ids=lambda e: e.command[-40:])
def test_first_judge_output_is_real(example: Example, tmp_path: Path) -> None:
    code, output = _run(example.command, tmp_path)
    assert _words(output).startswith(_words(example.output))
    if example.exit_code is not None:
        assert code == example.exit_code


def test_first_judge_has_examples() -> None:
    assert len(_examples(FIRST_JUDGE)) == 3


def test_the_annotated_spec_is_the_demo_spec() -> None:
    page = FIRST_JUDGE.read_text(encoding="utf-8")
    match = re.search(r'```yaml title="([^"]+)"\n(.*?)```', page, re.S)
    assert match is not None
    shown = yaml.safe_load(match.group(2))
    assert shown == yaml.safe_load((ROOT / match.group(1)).read_text(encoding="utf-8"))


def test_the_report_headline_is_real(tmp_path: Path) -> None:
    page = (PAGES / "read-the-report.md").read_text(encoding="utf-8")
    match = re.search(r"## The headline\n\n```text\n(.*?)```", page, re.S)
    assert match is not None
    _, output = _run(
        f"evidence-judge judge --trace {FORGED / 'trace.zip'} --har {FORGED / 'run.har'} "
        f"--answer-file {FORGED / 'answer.txt'} --spec {FORGED / 'spec.yaml'} --offline",
        tmp_path,
    )
    assert _words(match.group(1)) in _words(output)
