"""Tests for the api/worker-side session-audio helpers (Johnny-od1).

Covers the three responsibilities of ``app.services.session_audio``:
playback path resolution (with the URL-derived filename treated as hostile),
best-effort per-session deletion, and the orphan sweep that reconciles the
host bind mount against the ``bot_sessions`` table after a DB reset.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.db import Base
from app.db.models import BotSession, BotSessionSource, BotSessionStatus
from app.services.session_audio import (
    delete_session_audio,
    resolve_session_audio_file,
    session_audio_root,
    sweep_orphan_session_audio,
)
from johnny.voice_pipeline.audio_recorder import SESSION_AUDIO_DIR_ENV


@pytest.fixture
def audio_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "session-audio"
    root.mkdir()
    monkeypatch.setenv(SESSION_AUDIO_DIR_ENV, str(root))
    return root


@pytest.fixture
def engine() -> sa.Engine:
    eng = sa.create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=sa.pool.StaticPool,
    )
    Base.metadata.create_all(bind=eng, tables=[BotSession.__table__])  # type: ignore[list-item]
    return eng


@pytest.fixture
def db_session(engine: sa.Engine) -> Iterator[Session]:
    sess = Session(engine)
    try:
        yield sess
    finally:
        sess.close()


def _write_wav(root: Path, session_id: int, name: str) -> Path:
    session_dir = root / str(session_id)
    session_dir.mkdir(parents=True, exist_ok=True)
    path = session_dir / name
    path.write_bytes(b"RIFFfake")
    return path


def _age(path: Path, seconds: int = 3600) -> None:
    stamp = time.time() - seconds
    os.utime(path, (stamp, stamp))


# --- session_audio_root ------------------------------------------------------


def test_root_none_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(SESSION_AUDIO_DIR_ENV, raising=False)
    assert session_audio_root() is None
    monkeypatch.setenv(SESSION_AUDIO_DIR_ENV, "   ")
    assert session_audio_root() is None


# --- resolve_session_audio_file ----------------------------------------------


def test_resolve_returns_existing_file(audio_root: Path) -> None:
    path = _write_wav(audio_root, 5, "utt-1000-1.wav")
    assert resolve_session_audio_file(5, "utt-1000-1.wav") == path.resolve()


def test_resolve_missing_file_is_none(audio_root: Path) -> None:
    assert resolve_session_audio_file(5, "utt-9999-1.wav") is None


def test_resolve_without_root_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(SESSION_AUDIO_DIR_ENV, raising=False)
    assert resolve_session_audio_file(5, "utt-1000-1.wav") is None


@pytest.mark.parametrize(
    "hostile",
    [
        "../6/utt-1000-1.wav",
        "..%2F..%2Fetc%2Fpasswd",
        "a/b.wav",
        ".hidden.wav",
        "utt-1000-1.mp3",
        "",
        "x" * 200 + ".wav",
    ],
)
def test_resolve_rejects_hostile_filenames(audio_root: Path, hostile: str) -> None:
    with pytest.raises(ValueError):
        resolve_session_audio_file(5, hostile)


# --- delete_session_audio ----------------------------------------------------


def test_delete_removes_session_dir(audio_root: Path) -> None:
    _write_wav(audio_root, 5, "utt-1000-1.wav")
    delete_session_audio(5)
    assert not (audio_root / "5").exists()


def test_delete_is_noop_for_missing_dir(audio_root: Path) -> None:
    delete_session_audio(123)  # no raise


def test_delete_is_noop_without_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(SESSION_AUDIO_DIR_ENV, raising=False)
    delete_session_audio(5)  # no raise


# --- sweep_orphan_session_audio ----------------------------------------------


def test_sweep_removes_orphans_keeps_live(
    audio_root: Path, db_session: Session
) -> None:
    live = BotSession(
        source=BotSessionSource.BROWSER, status=BotSessionStatus.ENDED
    )
    db_session.add(live)
    db_session.commit()

    live_dir = _write_wav(audio_root, live.id, "utt-1-1.wav").parent
    orphan_dir = _write_wav(audio_root, live.id + 999, "utt-2-1.wav").parent
    _age(live_dir)
    _age(orphan_dir)

    removed = sweep_orphan_session_audio(db_session)

    assert removed == 1
    assert live_dir.exists()
    assert not orphan_dir.exists()


def test_sweep_skips_fresh_and_non_numeric_dirs(
    audio_root: Path, db_session: Session
) -> None:
    fresh_orphan = _write_wav(audio_root, 555, "utt-1-1.wav").parent  # mtime = now
    weird = audio_root / "not-a-session"
    weird.mkdir()
    _age(weird)

    removed = sweep_orphan_session_audio(db_session)

    assert removed == 0
    assert fresh_orphan.exists()
    assert weird.exists()


def test_sweep_noop_without_root(
    monkeypatch: pytest.MonkeyPatch, db_session: Session
) -> None:
    monkeypatch.delenv(SESSION_AUDIO_DIR_ENV, raising=False)
    assert sweep_orphan_session_audio(db_session) == 0
