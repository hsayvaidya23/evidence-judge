from __future__ import annotations

import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest

from evidence_judge.core.verdict import Outcome
from evidence_judge.report.json_report import write_json
from tests.unit.test_report_fixtures import make_verdict

ROOT = Path(__file__).parents[2]


def _pyproject() -> dict[str, Any]:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def test_otel_extra_ships_an_otlp_exporter() -> None:
    otel = _pyproject()["project"]["optional-dependencies"]["otel"]
    assert any(d.startswith("opentelemetry-exporter-otlp-proto-http") for d in otel)


def test_sdist_ships_what_the_test_suite_imports() -> None:
    include = set(_pyproject()["tool"]["hatch"]["build"]["targets"]["sdist"]["include"])
    assert {"src/evidence_judge", "tests", "scripts", "demo"} <= include


def test_docker_image_pins_bases_by_digest_and_installs_from_the_lockfile() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    bases = [line.split()[1] for line in dockerfile.splitlines() if line.startswith("FROM ")]
    assert bases
    assert all("@sha256:" in base for base in bases)
    assert "uv export --frozen" in dockerfile
    assert "--require-hashes" in dockerfile


def test_release_runs_the_full_ci_before_publishing() -> None:
    import yaml

    workflows = ROOT / ".github" / "workflows"
    ci = yaml.safe_load((workflows / "ci.yml").read_text(encoding="utf-8"))
    release = yaml.safe_load((workflows / "release.yml").read_text(encoding="utf-8"))
    assert "workflow_call" in ci[True]
    jobs = release["jobs"]
    [gate] = [name for name, job in jobs.items() if job.get("uses") == "$/.github/workflows/ci.yml"]
    assert gate in jobs["build"]["needs"]
    assert jobs["publish"]["needs"] == "build"


_USES = re.compile(r"^\s*(?:-\s+)?uses:\s*(\S+)(.*)$")
_PINNED = re.compile(r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$")
_VERSION_COMMENT = re.compile(r"^\s+# v\d+\.\d+\.\d+$")


def _uses_lines() -> list[tuple[str, str, str]]:
    files = [ROOT / "action.yml", *sorted((ROOT / ".github" / "workflows").glob("*.yml"))]
    found: list[tuple[str, str, str]] = []
    for path in files:
        for line in path.read_text(encoding="utf-8").splitlines():
            match = _USES.match(line)
            if match:
                found.append((path.name, match.group(1), match.group(2)))
    return found


def test_third_party_actions_are_pinned_to_a_commit_sha_with_a_version_comment() -> None:
    external = [(f, ref, rest) for f, ref, rest in _uses_lines() if not ref.startswith("$/")]
    assert {f for f, _, _ in external} >= {"action.yml", "ci.yml", "release.yml", "docs.yml"}
    for file, ref, rest in external:
        assert _PINNED.match(ref), f"{file}: {ref} is not pinned to a full commit SHA"
        assert _VERSION_COMMENT.match(rest), f"{file}: {ref} lacks a '# vX.Y.Z' comment"


def test_each_action_is_pinned_to_one_sha_everywhere() -> None:
    shas: dict[str, set[str]] = {}
    for _, ref, rest in _uses_lines():
        if not ref.startswith("$/"):
            name, _, sha = ref.partition("@")
            shas.setdefault(name, set()).add(f"{sha}{rest.strip()}")
    assert all(len(pins) == 1 for pins in shas.values()), shas


def test_in_repo_actions_and_workflows_use_the_self_repository_form() -> None:
    assert not [(f, ref) for f, ref, _ in _uses_lines() if ref.startswith("./")]


def _workflows() -> dict[str, dict[Any, Any]]:
    import yaml

    return {
        path.name: yaml.safe_load(path.read_text(encoding="utf-8"))
        for path in sorted((ROOT / ".github" / "workflows").glob("*.yml"))
    }


def test_checkouts_never_persist_credentials() -> None:
    for name, workflow in _workflows().items():
        for job_name, job in workflow["jobs"].items():
            for step in job.get("steps", []):
                if str(step.get("uses", "")).startswith("actions/checkout@"):
                    assert step.get("with", {}).get("persist-credentials") is False, (
                        name,
                        job_name,
                    )


def test_dependabot_keeps_action_pins_updated_after_a_cooldown() -> None:
    import yaml

    config = yaml.safe_load((ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8"))
    assert any(u["package-ecosystem"] == "github-actions" for u in config["updates"])
    assert all(u["cooldown"]["default-days"] >= 7 for u in config["updates"])


def test_release_attests_the_distributions_and_ships_an_sbom() -> None:
    jobs = _workflows()["release.yml"]["jobs"]
    build = jobs["build"]
    assert build["permissions"]["id-token"] == "write"
    assert build["permissions"]["attestations"] == "write"
    attests = [
        s["with"] for s in build["steps"] if str(s.get("uses", "")).startswith("actions/attest@")
    ]
    assert {"subject-path": "dist/*"} in attests
    assert any("sbom-path" in a for a in attests)
    assert any("--format cyclonedx" in s.get("run", "") for s in build["steps"])
    release = jobs["github-release"]
    assert release["permissions"] == {"contents": "write"}
    assert "dist/* sbom/*" in release["steps"][-1]["run"]


def test_changelog_check_runs_only_on_pull_requests_and_honours_the_skip_label() -> None:
    job = _workflows()["ci.yml"]["jobs"]["changelog"]
    assert job["if"] == "github.event_name == 'pull_request'"
    script = job["steps"][-1]["run"]
    assert "skip-changelog" in script
    assert "CHANGELOG.md" in script


def _action() -> dict[str, Any]:
    import yaml

    return yaml.safe_load((ROOT / "action.yml").read_text(encoding="utf-8"))


def test_action_documents_the_reasoning_file_format_the_loader_reads() -> None:
    description = _action()["inputs"]["reasoning-file"]["description"]
    assert "paragraph" in description
    assert "blank line" in description


def test_action_version_installs_from_the_repository_not_pypi() -> None:
    action = _action()
    install = next(s for s in action["runs"]["steps"] if s.get("name") == "Install evidence-judge")
    assert "git+https://github.com/hsayvaidya23/evidence-judge@" in install["run"]
    assert "==" not in install["run"]
    assert "tag" in action["inputs"]["version"]["description"]


def test_action_summary_goes_through_the_escaping_module() -> None:
    judge = next(s for s in _action()["runs"]["steps"] if s.get("id") == "judge")
    script: str = judge["run"]
    assert "-m evidence_judge.ci_summary" in script
    assert "GITHUB_STEP_SUMMARY" not in script
    assert "['summary']" not in script


def test_action_never_reuses_an_earlier_verdict() -> None:
    judge = next(s for s in _action()["runs"]["steps"] if s.get("id") == "judge")
    script: str = judge["run"]
    assert 'out=$(mktemp -d "$RUNNER_TEMP/evidence-judge.XXXXXX")' in script
    assert 'if [ "$code" -ge 3 ] ||' in script


def _check_inputs(tmp_path: Path, **inputs: str) -> tuple[int, str, dict[str, str]]:
    step = next(s for s in _action()["runs"]["steps"] if s.get("id") == "inputs")
    outputs = tmp_path / "outputs"
    outputs.touch()
    env = {"PATH": os.environ["PATH"], "GITHUB_OUTPUT": str(outputs)}
    env.update({f"EJ_{name.upper()}": value for name, value in inputs.items()})
    result = subprocess.run(
        ["bash", "-e", "-c", step["run"]], env=env, capture_output=True, text=True, check=False
    )
    written = dict(line.split("=", 1) for line in outputs.read_text().splitlines())
    return result.returncode, result.stdout, written


@pytest.mark.parametrize(
    ("given", "read"), [("true", "true"), ("True", "true"), ("FALSE", "false")]
)
def test_action_reads_boolean_inputs_in_any_case(tmp_path: Path, given: str, read: str) -> None:
    code, _, written = _check_inputs(
        tmp_path, offline=given, fail_on_review=given, upload_report="false"
    )
    assert code == 0
    assert written == {
        "offline": read,
        "fail-on-review": read,
        "upload-report": "false",
        "mode": "browser",
    }


@pytest.mark.parametrize("given", ["yes", "1", "", "true ", "on"])
def test_action_rejects_a_boolean_input_that_is_not_true_or_false(
    tmp_path: Path, given: str
) -> None:
    code, stdout, _ = _check_inputs(
        tmp_path, offline="true", fail_on_review=given, upload_report="false"
    )
    assert code != 0
    assert "::error::input fail-on-review must be true or false" in stdout


@pytest.mark.parametrize(("given", "read"), [("", "claude-code"), ("Codex", "codex")])
def test_action_session_mode_checks_its_format(tmp_path: Path, given: str, read: str) -> None:
    flags = {"offline": "true", "fail_on_review": "false", "upload_report": "false"}
    code, _, written = _check_inputs(
        tmp_path, **flags, session_transcript="s.jsonl", session_format=given
    )
    assert code == 0
    assert written["mode"] == "session" and written["session-format"] == read


@pytest.mark.parametrize(
    ("inputs", "error"),
    [
        ({"session_transcript": "s.jsonl", "session_format": "gemini"}, "session-format must be"),
        ({"session_transcript": "s.jsonl", "trace": "t.zip"}, "drop trace"),
        ({"session_transcript": "s.jsonl", "spec": "spec.yaml"}, "use session-spec"),
        ({"session_spec": "s.yaml"}, "session-spec needs session-transcript"),
        ({"session_transcript": "s.jsonl", "site": "shop.test"}, "spec and site"),
    ],
)
def test_action_rejects_mixed_or_unknown_session_inputs(
    tmp_path: Path, inputs: dict[str, str], error: str
) -> None:
    flags = {"offline": "true", "fail_on_review": "false", "upload_report": "false"}
    code, stdout, written = _check_inputs(tmp_path, **flags, **inputs)
    assert code != 0
    assert error in stdout
    assert "mode" not in written


@pytest.mark.parametrize(
    "site", ["shop.test", "HTTPS://Shop.test/", "shop.test:8000", "http://[::1]:8000", ""]
)
def test_action_accepts_a_site_url_or_host(tmp_path: Path, site: str) -> None:
    flags = {"offline": "true", "fail_on_review": "false", "upload_report": "false"}
    code, stdout, written = _check_inputs(tmp_path, **flags, trace="t.zip", site=site)
    assert code == 0, stdout
    assert written["mode"] == "browser"


@pytest.mark.parametrize(
    "site",
    [
        " ",
        "shop test",
        "https://shop.test/checkout",
        "shop.test?q=1",
        "ftp://shop.test",
        "user@shop.test",
        "https://",
        "shop.test:port",
    ],
)
def test_action_rejects_a_site_that_is_not_a_url_or_host(tmp_path: Path, site: str) -> None:
    flags = {"offline": "true", "fail_on_review": "false", "upload_report": "false"}
    code, stdout, written = _check_inputs(tmp_path, **flags, trace="t.zip", site=site)
    assert code != 0
    assert "::error::input site must be an http(s) URL or a host" in stdout
    assert "mode" not in written


def _fake_tools(tmp_path: Path) -> Path:
    """An `evidence-judge` that records its arguments and writes a passing verdict, and a `uv`
    whose tool python is this interpreter."""
    bin_dir = tmp_path / "bin"
    tool_python = tmp_path / "tools" / "evidence-judge" / "bin" / "python"
    for path in (bin_dir, tool_python.parent):
        path.mkdir(parents=True)
    fake = bin_dir / "evidence-judge"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        'printf "%s\\n" "$@" > "$EJ_ARGS"\n'
        'while [ $# -gt 0 ]; do [ "$1" = --out ] && cp "$EJ_VERDICT" "$2"; shift; done\n',
        encoding="utf-8",
    )
    uv = bin_dir / "uv"
    uv.write_text(f"#!/usr/bin/env bash\necho {tmp_path / 'tools'}\n", encoding="utf-8")
    tool_python.write_text(f'#!/usr/bin/env bash\nexec {sys.executable} "$@"\n', encoding="utf-8")
    for path in (fake, uv, tool_python):
        path.chmod(0o755)
    return bin_dir


def test_action_session_mode_runs_session_judge(tmp_path: Path) -> None:
    step = next(s for s in _action()["runs"]["steps"] if s.get("id") == "judge")
    bin_dir = _fake_tools(tmp_path)
    (tmp_path / "runner").mkdir()
    verdict = tmp_path / "verdict.json"
    write_json(make_verdict(Outcome.PASS), verdict)
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "RUNNER_TEMP": str(tmp_path / "runner"),
        "GITHUB_OUTPUT": str(tmp_path / "outputs"),
        "GITHUB_STEP_SUMMARY": str(tmp_path / "summary"),
        "PYTHONPATH": str(ROOT / "src"),
        "EJ_ARGS": str(tmp_path / "args"),
        "EJ_VERDICT": str(verdict),
        "EJ_MODE": "session",
        "EJ_SESSION_TRANSCRIPT": "rollout.jsonl",
        "EJ_SESSION_FORMAT": "codex",
        "EJ_SESSION_SPEC": "session.yaml",
        "EJ_ANSWER_FILE": "answer.txt",
        "EJ_OFFLINE": "true",
        "EJ_FAIL_ON_REVIEW": "false",
    }
    result = subprocess.run(
        ["bash", "-e", "-c", step["run"]], env=env, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr
    args = (tmp_path / "args").read_text(encoding="utf-8").splitlines()
    assert args[:2] == ["session", "judge"]
    joined = " ".join(args)
    assert "--codex rollout.jsonl" in joined and "--spec session.yaml" in joined
    assert "--answer-file answer.txt" in joined and "--offline" in args
    assert "--trace" not in args and "--html" in args


@pytest.mark.parametrize(("site", "passed"), [("https://shop.test:8443/", True), ("", False)])
def test_action_browser_mode_passes_the_site(tmp_path: Path, site: str, passed: bool) -> None:
    step = next(s for s in _action()["runs"]["steps"] if s.get("id") == "judge")
    assert step["env"]["EJ_SITE"] == "${{ inputs.site }}"
    assert "inputs.site" not in step["run"]
    bin_dir = _fake_tools(tmp_path)
    (tmp_path / "runner").mkdir()
    verdict = tmp_path / "verdict.json"
    write_json(make_verdict(Outcome.PASS), verdict)
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "RUNNER_TEMP": str(tmp_path / "runner"),
        "GITHUB_OUTPUT": str(tmp_path / "outputs"),
        "GITHUB_STEP_SUMMARY": str(tmp_path / "summary"),
        "PYTHONPATH": str(ROOT / "src"),
        "EJ_ARGS": str(tmp_path / "args"),
        "EJ_VERDICT": str(verdict),
        "EJ_MODE": "browser",
        "EJ_TRACE": "trace.zip",
        "EJ_SITE": site,
        "EJ_OFFLINE": "true",
        "EJ_FAIL_ON_REVIEW": "false",
    }
    result = subprocess.run(
        ["bash", "-e", "-c", step["run"]], env=env, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr
    args = (tmp_path / "args").read_text(encoding="utf-8").splitlines()
    assert args[0] == "judge" and "--trace" in args
    if passed:
        assert args[args.index("--site") + 1] == site
    else:
        assert "--site" not in args


def test_action_steps_use_the_checked_inputs() -> None:
    action = _action()
    steps = action["runs"]["steps"]
    assert steps[0]["id"] == "inputs"
    judge = next(s for s in steps if s.get("id") == "judge")
    assert judge["env"]["EJ_OFFLINE"] == "${{ steps.inputs.outputs.offline }}"
    assert judge["env"]["EJ_FAIL_ON_REVIEW"] == "${{ steps.inputs.outputs.fail-on-review }}"
    upload = steps[-1]
    assert upload["if"].startswith("always() && steps.inputs.outputs.upload-report == 'true'")
    assert str(upload["uses"]).startswith("actions/upload-artifact@")
    assert upload["with"]["path"] == "${{ steps.judge.outputs.report }}"
    assert action["inputs"]["upload-report"]["default"] == "false"


def test_action_api_key_input_never_clears_the_job_environment() -> None:
    action = _action()
    assert action["inputs"]["anthropic-api-key"]["default"] == ""
    judge = next(s for s in action["runs"]["steps"] if s.get("id") == "judge")
    assert judge["env"]["EJ_API_KEY"] == "${{ inputs.anthropic-api-key }}"
    assert "ANTHROPIC_API_KEY" not in judge["env"]
    script: str = judge["run"]
    assert 'if [ -n "$EJ_API_KEY" ]; then' in script
    assert script.index("export ANTHROPIC_API_KEY") < script.index("::stop-commands::")


def test_ci_exercises_the_action_failure_path() -> None:
    import yaml

    ci = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
    steps = ci["jobs"]["action"]["steps"]
    broken = next(s for s in steps if s.get("id") == "broken")
    assert broken["continue-on-error"] is True
    assert 'test "$BROKEN_STEP" = failure' in steps[-1]["run"]
    badflag = next(s for s in steps if s.get("id") == "badflag")
    assert badflag["continue-on-error"] is True
    assert badflag["with"]["offline"] == "yes"
    assert 'test "$BADFLAG_STEP" = failure' in steps[-1]["run"]
    badsite = next(s for s in steps if s.get("id") == "badsite")
    assert badsite["continue-on-error"] is True and "/" in badsite["with"]["site"].split("//")[1]
    assert 'test "$BADSITE_STEP" = failure' in steps[-1]["run"]
    honest = next(s for s in steps if s.get("id") == "honest")
    assert honest["with"]["site"]
    session = next(s for s in steps if s.get("id") == "session")
    assert "session-transcript" in session["with"]
    assert 'test "$SESSION" = pass' in steps[-1]["run"]
    assert 'test "$MIXED_STEP" = failure' in steps[-1]["run"]


def test_project_urls_point_at_the_docs_changelog_and_issues() -> None:
    urls = _pyproject()["project"]["urls"]
    config = (ROOT / "mkdocs.yml").read_text(encoding="utf-8")
    mkdocs = dict(re.findall(r"^(site_url|repo_url): (\S+)$", config, re.M))
    assert urls["Documentation"] == mkdocs["site_url"]
    assert urls["Repository"] == mkdocs["repo_url"]
    assert urls["Changelog"] == f"{mkdocs['repo_url']}/blob/main/CHANGELOG.md"
    assert urls["Issues"] == f"{mkdocs['repo_url']}/issues"
