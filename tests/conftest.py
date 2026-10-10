import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "appcode"))


@pytest.fixture(autouse=True)
def isolated_dirs(tmp_path, monkeypatch):
    """Never touch the real ~/.local/share/lizardking or cache."""
    monkeypatch.setenv("LIZARDKING_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("LIZARDKING_CACHE", str(tmp_path / "cache"))
    from services import store
    store.use_file(None)
    yield
    store.use_file(None)


def ffmpeg_available() -> bool:
    from shutil import which
    return bool(which(os.getenv("LIZARDKING_FFMPEG", "ffmpeg")) and which(os.getenv("LIZARDKING_FFPROBE", "ffprobe")))


needs_ffmpeg = pytest.mark.skipif(not ffmpeg_available(), reason="ffmpeg/ffprobe not installed")
