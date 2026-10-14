from __future__ import annotations

import stat
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import SecretStr

from evidence_judge.config import REDACTION_KEY_FILE, Settings, stable_redaction_key


def _settings(cache_dir: Path | None, key: str | None = None) -> Settings:
    return Settings.model_validate(
        {"cache_dir": cache_dir, "redaction_key": SecretStr(key) if key is not None else None}
    )


def test_explicit_key_wins_and_writes_nothing(tmp_path: Path) -> None:
    assert stable_redaction_key(_settings(tmp_path, "s3cret")) == b"s3cret"
    assert not (tmp_path / REDACTION_KEY_FILE).exists()


def test_no_key_and_no_cache_dir_returns_none() -> None:
    assert stable_redaction_key(_settings(None)) is None


def test_creates_private_key_file_and_reuses_it(tmp_path: Path) -> None:
    cache_dir = tmp_path / "cache"
    first = stable_redaction_key(_settings(cache_dir))
    assert first is not None
    assert len(first) == 32
    path = cache_dir / REDACTION_KEY_FILE
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stable_redaction_key(_settings(cache_dir)) == first
    assert [p.name for p in cache_dir.iterdir()] == [REDACTION_KEY_FILE]


def test_key_is_stable_across_processes(tmp_path: Path) -> None:
    code = (
        "import sys; from pathlib import Path; "
        "from evidence_judge.config import Settings, stable_redaction_key; "
        "key = stable_redaction_key(Settings.model_validate("
        "{'cache_dir': Path(sys.argv[1]), 'redaction_key': None})); "
        "print(key.hex())"
    )
    outputs = {
        subprocess.run(
            [sys.executable, "-c", code, str(tmp_path)],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        for _ in range(2)
    }
    assert len(outputs) == 1
    assert bytes.fromhex(outputs.pop()) == stable_redaction_key(_settings(tmp_path))


def test_corrupt_key_file_is_an_error(tmp_path: Path) -> None:
    (tmp_path / REDACTION_KEY_FILE).write_text("not-hex")
    with pytest.raises(ValueError, match="redaction key file"):
        stable_redaction_key(_settings(tmp_path))
