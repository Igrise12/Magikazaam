import json
from pathlib import Path
import shutil
import sqlite3
import subprocess

import pytest
from yt_dlp.utils import DownloadError

from magikazaam import catalog, importer
from magikazaam.fingerprint import FingerprintFailure
from magikazaam.importer import ImportFailure, ImportResult, normalize_playlist, import_playlist, validate_audio


PLAYLIST_ID = "PLabcdefghijklmno"
FIRST_ID = "abcdefghijk"
SECOND_ID = "lmnopqrstuv"


@pytest.mark.parametrize("value", [
    PLAYLIST_ID,
    f" https://www.youtube.com/playlist?list={PLAYLIST_ID} ",
    f"https://youtube.com/watch?v={FIRST_ID}&list={PLAYLIST_ID}",
    f"https://music.youtube.com/playlist?list={PLAYLIST_ID}",
])
def test_normalize_playlist(value):
    assert normalize_playlist(value) == f"https://www.youtube.com/playlist?list={PLAYLIST_ID}"


@pytest.mark.parametrize("value", [
    "", "short", "https://example.com/playlist?list=PLabcdefghijklmno",
    "https://youtube.com.evil.test/playlist?list=PLabcdefghijklmno",
    "https://user:password@youtube.com/playlist?list=PLabcdefghijklmno",
    "https://youtube.com:8000/playlist?list=PLabcdefghijklmno",
    "https://youtube.com:bad/playlist?list=PLabcdefghijklmno",
    "https://youtube.com/watch?v=abcdefghijk",
    "https://youtube.com/playlist?list=PLabcdefghijklmno&list=PLanotherplaylist",
    "file:///etc/passwd", "https://[invalid", "../relative/path",
])
def test_reject_other_sources(value):
    with pytest.raises(ImportFailure, match="playlist URL"):
        normalize_playlist(value)


@pytest.fixture
def fake_youtube(monkeypatch):
    config = {
        "entries": [{"id": FIRST_ID, "title": "First", "channel": "Creator"},
                    {"id": SECOND_ID, "title": "Second"}],
        "downloads": [],
        "options": [],
        "fail_ids": set(),
        "oversize_ids": set(),
    }

    class FakeYoutubeDL:
        def __init__(self, options):
            self.options = options
            config["options"].append(options)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def extract_info(self, url, download):
            if not download:
                if config.get("playlist_error"):
                    raise DownloadError("private playlist")
                return {"entries": config["entries"], "playlist_count": config.get("count")}
            video_id = url.split("v=")[-1]
            config["downloads"].append(video_id)
            directory = Path(self.options["outtmpl"]).parent
            if video_id in config["fail_ids"]:
                (directory / "failed.part").write_bytes(b"partial")
                raise DownloadError("sensitive download internals")
            if video_id in config["oversize_ids"]:
                (directory / "oversize.part").write_bytes(b"partial")
                self.options["progress_hooks"][0]({"downloaded_bytes": importer.MAX_BYTES + 1})
            (directory / f"{video_id}.m4a").write_bytes(b"audio")
            return {"title": f"Downloaded {video_id}", "channel": "Channel"}

    monkeypatch.setattr(importer, "YoutubeDL", FakeYoutubeDL)
    monkeypatch.setattr(importer, "check_prerequisites", lambda: None)
    monkeypatch.setattr(importer, "validate_audio", lambda path: None)
    monkeypatch.setattr(catalog, "fingerprint_audio", lambda path: iter([((1, 2, 3), 0)]))
    return config


def test_mixed_results_and_failed_partial_cleanup(fake_youtube, tmp_path):
    fake_youtube["fail_ids"].add(FIRST_ID)
    snapshots = []
    result = import_playlist(ImportResult(PLAYLIST_ID), tmp_path,
                              lambda result: snapshots.append((result.state, result.completed)), catalog_path=tmp_path / "catalog.sqlite3")
    assert result.state == "completed"
    assert result.total == result.completed == 2
    assert [track.status for track in result.tracks] == ["failed", "indexed"]
    assert "sensitive" not in result.tracks[0].error
    job_dir = tmp_path / result.import_id
    assert not list(job_dir.glob(".partial-*"))
    assert not list(job_dir.rglob("*.part"))
    assert not list(job_dir.rglob("*.m4a"))
    with catalog.open_catalog(tmp_path / "catalog.sqlite3") as connection:
        assert connection.execute("SELECT audio_blob FROM tracks").fetchone()[0] == b"audio"
    manifest = json.loads((job_dir / "manifest.json").read_text())
    assert manifest["state"] == "completed"
    assert manifest["tracks"][1]["title"] == f"Downloaded {SECOND_ID}"
    assert snapshots[-1] == ("completed", 2)
    assert ("running", 1) in snapshots
    assert fake_youtube["options"][0]["playlistend"] == 101
    assert all(options["js_runtimes"] == {"node": {}} for options in fake_youtube["options"])


@pytest.mark.parametrize("count,entries", [(None, 101), (101, 2)])
def test_oversized_playlist_rejected_before_download(fake_youtube, tmp_path, count, entries):
    fake_youtube["entries"] = [{"id": FIRST_ID}] * entries
    fake_youtube["count"] = count
    result = import_playlist(ImportResult(PLAYLIST_ID), tmp_path, catalog_path=tmp_path / "catalog.sqlite3")
    assert result.state == "failed"
    assert "100-entry" in result.error
    assert fake_youtube["downloads"] == []


def test_unavailable_entries_reported_without_download(fake_youtube, tmp_path):
    fake_youtube["entries"] = [None, {"id": "../../unsafe"}, {"id": SECOND_ID}]
    result = import_playlist(ImportResult(PLAYLIST_ID), tmp_path, catalog_path=tmp_path / "catalog.sqlite3")
    assert [track.status for track in result.tracks] == ["failed", "failed", "indexed"]
    assert fake_youtube["downloads"] == [SECOND_ID]


def test_oversized_download_removed_and_next_track_indexed(fake_youtube, tmp_path):
    fake_youtube["oversize_ids"].add(FIRST_ID)
    result = import_playlist(ImportResult(PLAYLIST_ID), tmp_path, catalog_path=tmp_path / "catalog.sqlite3")
    assert result.tracks[0].status == "failed"
    assert "50 MiB" in result.tracks[0].error
    assert result.tracks[1].status == "indexed"
    assert not list(tmp_path.rglob("*.part"))


@pytest.mark.parametrize("mode", ["empty", "private"])
def test_playlist_errors(fake_youtube, tmp_path, mode):
    if mode == "empty":
        fake_youtube["entries"] = []
    else:
        fake_youtube["playlist_error"] = True
    result = import_playlist(ImportResult(PLAYLIST_ID), tmp_path, catalog_path=tmp_path / "catalog.sqlite3")
    assert result.state == "failed"
    assert result.error
    assert fake_youtube["downloads"] == []


def test_corrupt_track_does_not_stop_next_track(fake_youtube, monkeypatch, tmp_path):
    def check(path):
        if FIRST_ID in path.name:
            raise ImportFailure("Audio could not be decoded.")
    monkeypatch.setattr(importer, "validate_audio", check)
    result = import_playlist(ImportResult(PLAYLIST_ID), tmp_path, catalog_path=tmp_path / "catalog.sqlite3")
    assert [track.status for track in result.tracks] == ["failed", "indexed"]
    assert not list(tmp_path.rglob(f"{FIRST_ID}.m4a"))


def test_manifest_write_failure_is_actionable(fake_youtube, monkeypatch, tmp_path):
    def disk_full(*args, **kwargs):
        raise OSError("disk full")
    monkeypatch.setattr(Path, "write_text", disk_full)
    with pytest.raises(ImportFailure, match="disk space"):
        import_playlist(ImportResult(PLAYLIST_ID), tmp_path, catalog_path=tmp_path / "catalog.sqlite3")
    assert fake_youtube["downloads"] == []


def test_unexpected_failure_marks_manifest_failed(fake_youtube, tmp_path):
    fake_youtube["entries"] = ["malformed playlist metadata"]
    result = import_playlist(ImportResult(PLAYLIST_ID), tmp_path, catalog_path=tmp_path / "catalog.sqlite3")
    assert result.state == "failed"
    manifest = json.loads((tmp_path / result.import_id / "manifest.json").read_text())
    assert manifest["state"] == "failed"


def test_repeat_import_and_duplicate_entries_skip_downloads(fake_youtube, tmp_path):
    database = tmp_path / "catalog.sqlite3"
    fake_youtube["entries"].append(fake_youtube["entries"][0])
    result = import_playlist(ImportResult(PLAYLIST_ID), tmp_path, catalog_path=database)
    assert [track.status for track in result.tracks] == ["indexed", "indexed", "skipped"]
    assert fake_youtube["downloads"] == [FIRST_ID, SECOND_ID]
    fake_youtube["downloads"].clear()
    second = import_playlist(ImportResult(PLAYLIST_ID), tmp_path, catalog_path=database)
    assert [track.status for track in second.tracks] == ["skipped"] * 3
    assert not fake_youtube["downloads"]
    assert len(catalog.list_tracks(database)) == 2
    assert not list(tmp_path.rglob("*.m4a"))


@pytest.mark.parametrize("failure", ["fingerprints", "database"])
def test_index_failure_continues_to_next_track(fake_youtube, tmp_path, monkeypatch, failure):
    database = tmp_path / "catalog.sqlite3"
    if failure == "fingerprints":
        def fingerprints(path):
            yield ((1, 2, 3), 0)
            if FIRST_ID in path.name:
                raise FingerprintFailure("Could not generate fingerprints.")
        monkeypatch.setattr(catalog, "fingerprint_audio", fingerprints)
    else:
        with catalog.open_catalog(database, initialize=True) as connection:
            connection.execute("""CREATE TRIGGER reject_posting BEFORE INSERT ON fingerprints
                WHEN NEW.track_id = 'abcdefghijk'
                BEGIN SELECT RAISE(ABORT, 'internal database details'); END""")
    result = import_playlist(ImportResult(PLAYLIST_ID), tmp_path, catalog_path=database)
    assert result.state == "completed"
    assert [track.status for track in result.tracks] == ["failed", "indexed"]
    assert result.tracks[0].error and "internal database details" not in result.tracks[0].error
    assert [track["video_id"] for track in catalog.list_tracks(database)] == [SECOND_ID]
    with catalog.open_catalog(database) as connection:
        assert not connection.execute("SELECT 1 FROM fingerprints WHERE track_id = ?", (FIRST_ID,)).fetchall()
    assert not list(tmp_path.rglob("*.m4a"))


def test_profile_mismatch_blocks_before_youtube_access(fake_youtube, tmp_path):
    database = tmp_path / "catalog.sqlite3"
    with catalog.open_catalog(database, initialize=True) as connection:
        with connection:
            connection.execute("UPDATE catalog_meta SET profile = '{}' ")
    result = import_playlist(ImportResult(PLAYLIST_ID), tmp_path, catalog_path=database)
    assert result.state == "failed" and "Rebuild" in result.error
    assert not fake_youtube["options"]
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT profile FROM catalog_meta").fetchone()[0] == "{}"


def test_existing_staging_is_untouched(fake_youtube, tmp_path):
    old = tmp_path / "old-import" / "001"
    old.mkdir(parents=True)
    audio = old / "audio.m4a"
    audio.write_bytes(b"old staged audio")
    import_playlist(ImportResult(PLAYLIST_ID), tmp_path, catalog_path=tmp_path / "catalog.sqlite3")
    assert audio.read_bytes() == b"old staged audio"


def test_missing_prerequisite_is_actionable(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None if name == "node" else f"/usr/bin/{name}")
    with pytest.raises(ImportFailure, match="Install node"):
        importer.check_prerequisites()


@pytest.mark.parametrize("info", [{"duration": 901}, {"duration": 0}, {"duration": float("nan")},
                                 {"is_live": True}, {"live_status": "is_upcoming"}])
def test_duration_filter_rejects_invalid_or_live_tracks(info):
    assert importer._duration_filter(info)


@pytest.fixture
def audio_file(tmp_path):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("FFmpeg and ffprobe are required for real decoder checks")
    def create(suffix=".m4a", codec="aac"):
        path = tmp_path / f"test{suffix}"
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i",
                        "sine=frequency=440:duration=0.25", "-c:a", codec, str(path)], check=True)
        return path
    return create


@pytest.mark.parametrize("suffix,codec", [(".m4a", "aac"), (".webm", "libopus")])
def test_validate_real_supported_audio(audio_file, suffix, codec):
    validate_audio(audio_file(suffix, codec))


def test_validate_rejects_corrupt_audio(tmp_path):
    path = tmp_path / "broken.m4a"
    path.write_bytes(b"this is not audio")
    with pytest.raises(ImportFailure, match="could not be decoded"):
        validate_audio(path)


def test_validate_rejects_empty_and_oversized_files(tmp_path):
    path = tmp_path / "size.m4a"
    path.touch()
    with pytest.raises(ImportFailure, match="nonempty"):
        validate_audio(path)
    with path.open("wb") as output:
        output.truncate(importer.MAX_BYTES + 1)
    with pytest.raises(ImportFailure, match="50 MiB"):
        validate_audio(path)


def test_validate_rejects_unsupported_codec(audio_file):
    with pytest.raises(ImportFailure, match="AAC"):
        validate_audio(audio_file(".m4a", "alac"))


def test_validate_checks_actual_duration(audio_file, monkeypatch):
    path = audio_file()
    monkeypatch.setattr(importer, "MAX_DURATION", 0.1)
    with pytest.raises(ImportFailure, match="duration"):
        validate_audio(path)


def test_validate_rejects_decoder_failure_after_valid_probe(audio_file, monkeypatch):
    path = audio_file()
    run = subprocess.run
    def fail_decoder(command, **options):
        if command[0] == "ffmpeg":
            raise subprocess.CalledProcessError(1, command)
        return run(command, **options)
    monkeypatch.setattr(subprocess, "run", fail_decoder)
    with pytest.raises(ImportFailure, match="could not be decoded"):
        validate_audio(path)
