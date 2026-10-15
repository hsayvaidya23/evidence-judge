from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

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


def test_ci_exercises_the_action_failure_path() -> None:
    import yaml

    ci = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
    steps = ci["jobs"]["action"]["steps"]
    broken = next(s for s in steps if s.get("id") == "broken")
    assert broken["continue-on-error"] is True
    assert 'test "$BROKEN_STEP" = failure' in steps[-1]["run"]


def test_project_urls_point_at_the_docs_changelog_and_issues() -> None:
    urls = _pyproject()["project"]["urls"]
    config = (ROOT / "mkdocs.yml").read_text(encoding="utf-8")
    mkdocs = dict(re.findall(r"^(site_url|repo_url): (\S+)$", config, re.M))
    assert urls["Documentation"] == mkdocs["site_url"]
    assert urls["Repository"] == mkdocs["repo_url"]
    assert urls["Changelog"] == f"{mkdocs['repo_url']}/blob/main/CHANGELOG.md"
    assert urls["Issues"] == f"{mkdocs['repo_url']}/issues"
