from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import shutil
import sqlite3
import subprocess
from threading import Barrier

import pytest

from magikazaam import catalog
from magikazaam.fingerprint import FingerprintFailure, fingerprint_audio, fingerprint_profile
from magikazaam.importer import TrackResult


FIRST_ID = "abcdefghijk"
SECOND_ID = "lmnopqrstuv"


def track(video_id=FIRST_ID):
    return TrackResult(video_id, "Song", "Artist", f"https://www.youtube.com/watch?v={video_id}")


@pytest.fixture
def source(monkeypatch, tmp_path):
    audio = tmp_path / "song.m4a"
    audio.write_bytes(b"original source audio")
    monkeypatch.setattr(catalog, "fingerprint_audio", lambda path: iter([((1, 2, 3), 0), ((1, 2, 3), 7)]))
    return audio


def test_blob_metadata_postings_lookup_and_delete(source, tmp_path):
    database = tmp_path / "catalog.sqlite3"
    with catalog.open_catalog(database, initialize=True) as connection:
        assert catalog.index_track(connection, track(), source)
        row = connection.execute("SELECT * FROM tracks").fetchone()
        assert row["audio_blob"] == source.read_bytes()
        assert row["audio_format"] == ".m4a"
        assert row["channel"] == "Artist"
        assert connection.execute("SELECT profile FROM catalog_meta").fetchone()[0] == fingerprint_profile()
        assert [tuple(row) for row in connection.execute(
            "SELECT track_id, anchor_frame FROM fingerprints WHERE hash_key = ? ORDER BY anchor_frame",
            (catalog.encode_key((1, 2, 3)),),
        )] == [(FIRST_ID, 0), (FIRST_ID, 7)]
        plan = connection.execute("EXPLAIN QUERY PLAN SELECT * FROM fingerprints WHERE hash_key = 1").fetchone()[3]
        assert "fingerprints_hash" in plan
        assert not connection.execute("PRAGMA index_list(fingerprints)").fetchone()[2]
        with pytest.raises(sqlite3.IntegrityError):
            with connection:
                connection.execute("INSERT INTO fingerprints VALUES (1, 'missing', 0)")
    assert catalog.list_tracks(database) == [{"video_id": FIRST_ID, "title": "Song", "channel": "Artist",
                                            "source_url": f"https://www.youtube.com/watch?v={FIRST_ID}"}]
    with catalog.open_catalog(database, initialize=True) as connection:
        with connection:
            connection.execute("DELETE FROM tracks WHERE video_id = ?", (FIRST_ID,))
        assert connection.execute("SELECT count(*) FROM fingerprints").fetchone()[0] == 0
    assert catalog.list_tracks(database) == []


def test_key_encoding_boundaries_and_collisions():
    keys = [(anchor, target, delta) for anchor in (1, 2, 511, 512)
            for target in range(1, 513) for delta in range(1, 51)]
    assert len({catalog.encode_key(key) for key in keys}) == len(keys)
    for key in keys:
        packed = catalog.encode_key(key)
        assert (packed >> 16, (packed >> 6) & 1023, packed & 63) == key


@pytest.mark.parametrize("key", [(0, 1, 1), (513, 1, 1), (1, 0, 1), (1, 513, 1), (1, 1, 0), (1, 1, 51)])
def test_key_encoding_rejects_out_of_range(key):
    with pytest.raises(catalog.CatalogFailure, match="ranges"):
        catalog.encode_key(key)


@pytest.mark.parametrize("profile", [None, "broken-json", '{"version":999}'])
def test_incompatible_profile_preserves_existing_audio(source, tmp_path, profile):
    database = tmp_path / "catalog.sqlite3"
    with catalog.open_catalog(database, initialize=True) as connection:
        catalog.index_track(connection, track(), source)
        with connection:
            if profile is None:
                connection.execute("DELETE FROM catalog_meta")
            else:
                connection.execute("UPDATE catalog_meta SET profile = ?", (profile,))
        with pytest.raises(catalog.CatalogFailure, match="Rebuild"):
            catalog.index_track(connection, track(SECOND_ID), source)
    for initialize in (True, False):
        with pytest.raises(catalog.CatalogFailure, match="Rebuild"):
            with catalog.open_catalog(database, initialize=initialize):
                pass
    with pytest.raises(catalog.CatalogFailure, match="Rebuild"):
        catalog.list_tracks(database)
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT audio_blob FROM tracks").fetchone()[0] == source.read_bytes()
        assert connection.execute("SELECT count(*) FROM fingerprints").fetchone()[0] == 2


def test_missing_metadata_is_not_silently_initialized(tmp_path):
    database = tmp_path / "catalog.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE old_data (value TEXT)")
        connection.execute("INSERT INTO old_data VALUES ('preserve me')")
    with pytest.raises(catalog.CatalogFailure, match="Rebuild"):
        with catalog.open_catalog(database, initialize=True):
            pass
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT value FROM old_data").fetchone()[0] == "preserve me"
        assert not connection.execute("SELECT name FROM sqlite_master WHERE name = 'catalog_meta'").fetchall()


@pytest.mark.parametrize("failure", ["fingerprints", "database", "empty"])
def test_failed_track_rolls_back_without_losing_previous_track(source, tmp_path, monkeypatch, failure):
    with catalog.open_catalog(tmp_path / "catalog.sqlite3", initialize=True) as connection:
        catalog.index_track(connection, track(), source)
        if failure == "fingerprints":
            def broken(path):
                yield ((1, 2, 3), 0)
                raise FingerprintFailure("Fingerprint generation failed")
            monkeypatch.setattr(catalog, "fingerprint_audio", broken)
        elif failure == "database":
            connection.execute("""CREATE TRIGGER reject_posting BEFORE INSERT ON fingerprints
                WHEN NEW.track_id = 'lmnopqrstuv' AND NEW.anchor_frame = 7
                BEGIN SELECT RAISE(ABORT, 'internal database failure'); END""")
        else:
            monkeypatch.setattr(catalog, "fingerprint_audio", lambda path: iter(()))
        with pytest.raises(catalog.CatalogFailure) as error:
            catalog.index_track(connection, track(SECOND_ID), source)
        assert "internal database failure" not in str(error.value)
        assert not catalog.has_track(connection, SECOND_ID)
        assert connection.execute("SELECT count(*) FROM tracks").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM fingerprints").fetchone()[0] == 2


def test_duplicate_does_not_read_audio_again(source, tmp_path, monkeypatch):
    with catalog.open_catalog(tmp_path / "catalog.sqlite3", initialize=True) as connection:
        assert catalog.index_track(connection, track(), source)
        source.unlink()
        monkeypatch.setattr(catalog, "fingerprint_audio", lambda path: pytest.fail("duplicate was fingerprinted"))
        assert not catalog.index_track(connection, track(), source)
        assert connection.execute("SELECT count(*) FROM fingerprints").fetchone()[0] == 2


def test_concurrent_imports_commit_only_one_copy(source, tmp_path):
    database = tmp_path / "catalog.sqlite3"
    barrier = Barrier(2)
    def index():
        barrier.wait(timeout=5)
        with catalog.open_catalog(database, initialize=True) as connection:
            return catalog.index_track(connection, track(), source)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: index(), range(2)))
    assert sorted(results) == [False, True]
    with catalog.open_catalog(database) as connection:
        assert connection.execute("SELECT count(*) FROM fingerprints").fetchone()[0] == 2


def test_absent_catalog_does_not_create_files(tmp_path):
    assert catalog.list_tracks(tmp_path / "missing" / "catalog.sqlite3") == []
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("suffix,codec", [(".m4a", "aac"), (".webm", "libopus")])
def test_generated_audio_uses_real_pipeline(tmp_path, suffix, codec):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("FFmpeg/ffprobe required for real indexing")
    audio = tmp_path / f"real{suffix}"
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=0.5", "-c:a", codec, str(audio)], check=True)
    expected = [(catalog.encode_key(key), frame) for key, frame in fingerprint_audio(audio)]
    assert expected
    with catalog.open_catalog(tmp_path / "catalog.sqlite3", initialize=True) as connection:
        assert catalog.index_track(connection, track(), audio)
        actual = [tuple(row) for row in connection.execute("SELECT hash_key, anchor_frame FROM fingerprints ORDER BY rowid")]
        assert actual == expected
        assert connection.execute("SELECT audio_blob FROM tracks").fetchone()[0] == audio.read_bytes()
