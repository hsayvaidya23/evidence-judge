"""CLI behaviour that depends on settings: cache location, the redaction key and privacy notices."""

from __future__ import annotations

import os
import stat
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

import evidence_judge.cli
from evidence_judge._cli_common import load_redactor
from evidence_judge.cli import ExitCode, app
from evidence_judge.config import (
    ENV_FILE_VAR,
    REDACTION_KEY_FILE,
    EnvFileError,
    Settings,
    default_cache_dir,
    read_settings,
)
from evidence_judge.mcp import Roots, ServerConfig
from evidence_judge.mcp.service import settings_of
from tests.unit.test_cli import Calls, install

runner = CliRunner()
GOLDEN = Path(__file__).parents[1] / "fixtures" / "runs" / "honest_success"


@pytest.fixture
def calls() -> Calls:
    return Calls()


@pytest.fixture
def evidence(tmp_path: Path) -> Path:
    path = tmp_path / "run.har"
    path.write_text("{}", encoding="utf-8")
    return path


@pytest.fixture
def read_only_dir(tmp_path: Path) -> Iterator[Path]:
    path = tmp_path / "ro"
    path.mkdir()
    path.chmod(stat.S_IRUSR | stat.S_IXUSR)
    if os.access(path, os.W_OK):
        pytest.skip("cannot make a read-only directory here (running as root?)")
    yield path
    path.chmod(stat.S_IRWXU)


@pytest.fixture
def no_env_key(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("EVJ_REDACTION_KEY", "EVJ_CACHE_DIR", "EVJ_SCREENSHOTS_TO_LLM", "EVJ_REDACT"):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize(
    ("platform", "env", "expected"),
    [
        ("linux", {}, Path("/home/u/.cache/evidence-judge")),
        ("linux", {"XDG_CACHE_HOME": "/xdg"}, Path("/xdg/evidence-judge")),
        ("linux", {"XDG_CACHE_HOME": "relative/ignored"}, Path("/home/u/.cache/evidence-judge")),
        ("darwin", {}, Path("/home/u/Library/Caches/evidence-judge")),
        ("darwin", {"XDG_CACHE_HOME": "/xdg"}, Path("/xdg/evidence-judge")),
        ("win32", {"LOCALAPPDATA": "/appdata"}, Path("/appdata/evidence-judge")),
        ("win32", {}, Path("/home/u/AppData/Local/evidence-judge")),
    ],
)
def test_default_cache_dir_is_per_user(
    monkeypatch: pytest.MonkeyPatch, platform: str, env: dict[str, str], expected: Path
) -> None:
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(Path, "home", lambda: Path("/home/u"))
    for name in ("XDG_CACHE_HOME", "LOCALAPPDATA"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    assert default_cache_dir() == expected


def test_settings_cache_dir_defaults_to_user_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, no_env_key: None
) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    settings = Settings.model_validate({})
    assert settings.cache_dir == tmp_path / "evidence-judge"
    assert settings.cache_dir is not None and settings.cache_dir.is_absolute()


def test_offline_judge_works_in_read_only_cwd(
    monkeypatch: pytest.MonkeyPatch, read_only_dir: Path, no_env_key: None
) -> None:
    monkeypatch.chdir(read_only_dir)
    monkeypatch.setenv("HOME", str(read_only_dir))
    monkeypatch.setenv("XDG_CACHE_HOME", str(read_only_dir / "cache"))
    result = runner.invoke(
        app,
        [
            "judge",
            "--trace",
            str(GOLDEN / "trace.zip"),
            "--har",
            str(GOLDEN / "run.har"),
            "--answer-file",
            str(GOLDEN / "answer.txt"),
            "--spec",
            str(GOLDEN / "spec.yaml"),
            "--offline",
        ],
    )
    assert result.exit_code == ExitCode.PASS, result.output
    assert list(read_only_dir.iterdir()) == []


def test_offline_judge_does_not_create_a_redaction_key(
    monkeypatch: pytest.MonkeyPatch, calls: Calls, evidence: Path, tmp_path: Path
) -> None:
    install(monkeypatch, calls)
    monkeypatch.delenv("EVJ_REDACTION_KEY")
    monkeypatch.setenv("EVJ_CACHE_DIR", str(tmp_path / "cache"))
    result = runner.invoke(app, ["judge", "--har", str(evidence), "--offline"])
    assert result.exit_code == ExitCode.PASS, result.output
    assert not (tmp_path / "cache").exists()


def test_online_judge_creates_the_redaction_key_in_the_cache(
    monkeypatch: pytest.MonkeyPatch, calls: Calls, evidence: Path, tmp_path: Path
) -> None:
    install(monkeypatch, calls)
    monkeypatch.delenv("EVJ_REDACTION_KEY")
    monkeypatch.setenv("EVJ_CACHE_DIR", str(tmp_path / "cache"))
    result = runner.invoke(app, ["judge", "--har", str(evidence)])
    assert result.exit_code == ExitCode.PASS, result.output
    assert (tmp_path / "cache" / REDACTION_KEY_FILE).is_file()


def test_online_judge_with_unwritable_cache_falls_back_to_a_process_key(
    monkeypatch: pytest.MonkeyPatch, calls: Calls, evidence: Path, read_only_dir: Path
) -> None:
    install(monkeypatch, calls)
    monkeypatch.delenv("EVJ_REDACTION_KEY")
    monkeypatch.setenv("EVJ_CACHE_DIR", str(read_only_dir / "cache"))
    result = runner.invoke(app, ["judge", "--har", str(evidence)])
    assert result.exit_code == ExitCode.PASS, result.output
    warnings = [line for line in result.output.splitlines() if "redaction key" in line]
    assert len(warnings) == 1
    assert "per-process key" in warnings[0]
    assert calls.run is not None and "[REDACTED:email:" in (calls.run.agent_answer or "")


def test_redactor_without_persistence_never_touches_the_cache(tmp_path: Path) -> None:
    settings = Settings.model_validate({"cache_dir": tmp_path / "cache", "redaction_key": None})
    load_redactor(settings, persistent=False)
    assert not (tmp_path / "cache").exists()


def test_corrupt_key_file_is_a_usage_error_for_judge(
    monkeypatch: pytest.MonkeyPatch, calls: Calls, evidence: Path, tmp_path: Path
) -> None:
    install(monkeypatch, calls)
    monkeypatch.delenv("EVJ_REDACTION_KEY")
    (tmp_path / REDACTION_KEY_FILE).write_text("not hex", encoding="ascii")
    (tmp_path / REDACTION_KEY_FILE).chmod(0o600)
    monkeypatch.setenv("EVJ_CACHE_DIR", str(tmp_path))
    result = runner.invoke(app, ["judge", "--har", str(evidence)])
    assert result.exit_code == ExitCode.USAGE, result.output
    assert "corrupt" in result.output and "Traceback" not in result.output


def test_inspect_ignores_a_corrupt_key_file(
    monkeypatch: pytest.MonkeyPatch, calls: Calls, evidence: Path, tmp_path: Path
) -> None:
    install(monkeypatch, calls)
    monkeypatch.delenv("EVJ_REDACTION_KEY")
    (tmp_path / REDACTION_KEY_FILE).write_text("not hex", encoding="ascii")
    monkeypatch.setenv("EVJ_CACHE_DIR", str(tmp_path))
    result = runner.invoke(app, ["inspect", "--har", str(evidence)])
    assert result.exit_code == 0, result.output


def test_inspect_invalid_settings_exit_3(
    monkeypatch: pytest.MonkeyPatch, calls: Calls, evidence: Path
) -> None:
    install(monkeypatch, calls)
    monkeypatch.setenv("EVJ_MAX_BODY_CHARS", "lots")
    result = runner.invoke(app, ["inspect", "--har", str(evidence)])
    assert result.exit_code == ExitCode.USAGE, result.output
    assert "invalid settings" in result.output and "Traceback" not in result.output


def test_inspect_internal_error_exit_4(
    monkeypatch: pytest.MonkeyPatch, calls: Calls, evidence: Path
) -> None:
    install(monkeypatch, calls)

    def boom(*_: object) -> None:
        raise RuntimeError("summary broke")

    monkeypatch.setattr(evidence_judge.cli, "summarize_evidence", boom)
    result = runner.invoke(app, ["inspect", "--har", str(evidence)])
    assert result.exit_code == ExitCode.INTERNAL, result.output
    assert "internal error: RuntimeError: summary broke" in result.output
    assert "Traceback" not in result.output
    debug = runner.invoke(app, ["inspect", "--har", str(evidence), "--debug"])
    assert isinstance(debug.exception, RuntimeError)


def test_screenshots_are_not_sent_to_the_llm_by_default(no_env_key: None) -> None:
    assert Settings.model_validate({}).screenshots_to_llm is False


def test_notice_when_screenshots_go_to_the_llm(
    monkeypatch: pytest.MonkeyPatch, calls: Calls, evidence: Path
) -> None:
    install(monkeypatch, calls)
    monkeypatch.setenv("EVJ_SCREENSHOTS_TO_LLM", "true")
    result = runner.invoke(app, ["judge", "--har", str(evidence)])
    notices = [line for line in result.output.splitlines() if "screenshots" in line]
    assert len(notices) == 1 and "unredacted" in notices[0]
    offline = runner.invoke(app, ["judge", "--har", str(evidence), "--offline"])
    assert "unredacted" not in offline.output
    quiet = runner.invoke(app, ["judge", "--har", str(evidence), "-q"])
    assert quiet.output == ""


def test_no_screenshot_notice_by_default(
    monkeypatch: pytest.MonkeyPatch, calls: Calls, evidence: Path
) -> None:
    install(monkeypatch, calls)
    monkeypatch.delenv("EVJ_SCREENSHOTS_TO_LLM", raising=False)
    result = runner.invoke(app, ["judge", "--har", str(evidence)])
    assert "unredacted" not in result.output


def test_offline_bench_does_not_create_a_redaction_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, no_env_key: None
) -> None:
    monkeypatch.setenv("EVJ_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    result = runner.invoke(app, ["bench", str(GOLDEN.parent), "--seeds", "1", "-q"])
    assert result.exit_code == ExitCode.PASS, result.output
    assert not (tmp_path / "cache" / REDACTION_KEY_FILE).exists()


def test_blank_secrets_in_env_mean_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    from evidence_judge.config import Settings

    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("EVJ_REDACTION_KEY", "   ")
    settings = Settings()
    assert settings.anthropic_api_key is None
    assert settings.redaction_key is None


@pytest.fixture
def hostile_env_in_cwd(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    for name in ("EVJ_REDACT", "EVJ_REVIEW_COVERAGE_THRESHOLD", "EVJ_MAX_BODY_CHARS", ENV_FILE_VAR):
        monkeypatch.delenv(name, raising=False)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".env").write_text(
        "EVJ_REDACT=false\nEVJ_REVIEW_COVERAGE_THRESHOLD=0\nEVJ_MAX_BODY_CHARS=lots\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(workspace)
    return workspace / ".env"


def test_a_dotenv_in_the_working_directory_is_never_read(hostile_env_in_cwd: Path) -> None:
    settings = read_settings()
    assert settings.redact is True
    assert settings.review_coverage_threshold == 0.6
    assert Settings().redact is True


def test_the_cli_ignores_a_dotenv_in_the_working_directory(
    monkeypatch: pytest.MonkeyPatch, calls: Calls, evidence: Path, hostile_env_in_cwd: Path
) -> None:
    install(monkeypatch, calls)
    result = runner.invoke(app, ["judge", "--har", str(evidence)])
    assert result.exit_code == ExitCode.PASS, result.output
    assert "redaction disabled" not in result.output


def test_the_mcp_server_ignores_a_dotenv_in_the_working_directory(
    hostile_env_in_cwd: Path, tmp_path: Path
) -> None:
    settings = settings_of(ServerConfig(roots=Roots.of([tmp_path])))
    assert settings.redact is True
    assert settings.review_coverage_threshold == 0.6


def test_env_file_option_is_read(
    monkeypatch: pytest.MonkeyPatch, calls: Calls, evidence: Path, hostile_env_in_cwd: Path
) -> None:
    install(monkeypatch, calls)
    result = runner.invoke(
        app, ["--env-file", str(hostile_env_in_cwd), "inspect", "--har", str(evidence)]
    )
    assert result.exit_code == ExitCode.USAGE, result.output
    assert "EVJ_MAX_BODY_CHARS" in result.output


def test_env_file_variable_is_read_and_real_variables_win(
    monkeypatch: pytest.MonkeyPatch, hostile_env_in_cwd: Path
) -> None:
    monkeypatch.setenv(ENV_FILE_VAR, str(hostile_env_in_cwd))
    monkeypatch.setenv("EVJ_MAX_BODY_CHARS", "100")
    settings = read_settings()
    assert settings.redact is False
    assert settings.review_coverage_threshold == 0
    assert settings.max_body_chars == 100


def test_a_missing_env_file_is_a_usage_error(
    monkeypatch: pytest.MonkeyPatch, calls: Calls, evidence: Path, tmp_path: Path
) -> None:
    install(monkeypatch, calls)
    monkeypatch.setenv(ENV_FILE_VAR, str(tmp_path / "missing.env"))
    with pytest.raises(EnvFileError):
        read_settings()
    result = runner.invoke(app, ["inspect", "--har", str(evidence)])
    assert result.exit_code == ExitCode.USAGE, result.output
    assert ENV_FILE_VAR in result.output and "Traceback" not in result.output
    missing = runner.invoke(
        app, ["--env-file", str(tmp_path / "missing.env"), "inspect", "--har", str(evidence)]
    )
    assert missing.exit_code == ExitCode.USAGE, missing.output
