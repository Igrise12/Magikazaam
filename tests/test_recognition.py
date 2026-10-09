from dataclasses import replace
from pathlib import Path
import sqlite3
import subprocess
from threading import Event, get_ident

from fastapi.testclient import TestClient
import numpy as np
import pytest

from magikazaam import api, catalog, fingerprint, recognition
from magikazaam.fingerprint import FingerprintFailure, FingerprintProcessingFailure
from magikazaam.recognition import Candidate, score_query, supported_match
from recognition_cases import encode, generated_song, measure_cases


@pytest.fixture
def connection(tmp_path):
    with catalog.open_catalog(tmp_path / "catalog.sqlite3", initialize=True) as connection:
        connection.execute("INSERT INTO tracks VALUES (?, ?, ?, ?, ?, ?)", ("track000000", "Song", None, "https://example.test/song", b"source", ".m4a"))
        connection.commit()
        yield connection


def posting(connection, key, frame, track="track000000"):
    connection.execute("INSERT INTO fingerprints VALUES (?, ?, ?)", (catalog.encode_key(key), track, frame))


def test_offset_neighborhood_deduplicates_postings_and_keeps_unmatched_denominator(connection):
    query = [((1, 2, 1), 10), ((2, 3, 2), 20), ((3, 4, 3), 30), ((4, 5, 4), 40)]
    for _ in range(5):
        posting(connection, query[0][0], 109)
        posting(connection, query[0][0], 110)
    posting(connection, query[1][0], 120)
    posting(connection, query[2][0], 131)
    assert score_query(connection, query) == [Candidate("track000000", 3, 0.75, 100)]
    assert score_query(connection, query + [query[0]]) == score_query(connection, query)


def test_offsets_outside_neighborhood_do_not_join_and_negative_offsets_work(connection):
    query = [((1, 2, 1), 10), ((2, 3, 2), 20)]
    posting(connection, query[0][0], 0)
    posting(connection, query[1][0], 13)
    assert score_query(connection, query)[0].aligned_votes == 1
    assert score_query(connection, query)[0].offset_frame < 0


def test_key_lookup_batches_more_than_sqlite_variable_limit(connection):
    query = [((1, index % 512 + 1, 1 + index // 512), index) for index in range(1200)]
    for key, frame in query:
        posting(connection, key, frame + 100)
    connection.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 500)
    assert score_query(connection, query)[0].aligned_votes == 1200


def test_thresholds_include_boundaries_and_reject_weak_evidence_and_ties():
    best = Candidate("a", recognition.MIN_ALIGNED_VOTES, recognition.MIN_SUPPORT_RATIO, 100)
    assert supported_match([best]) == best
    assert supported_match([replace(best, aligned_votes=best.aligned_votes - 1)]) is None
    assert supported_match([replace(best, support_ratio=best.support_ratio / 2)]) is None
    assert supported_match([best, replace(best, track_id="b")]) is None
    assert supported_match([replace(best, support_ratio=0.5), replace(best, track_id="b", support_ratio=0.499)]) is None
    assert supported_match([]) is None


@pytest.mark.parametrize("suffix", [".m4a", ".webm"])
def test_real_query_decoding_and_no_catalog_do_not_create_database(tmp_path, suffix):
    path = encode(generated_song(12, 10), tmp_path / f"query{suffix}", streaming=suffix == ".webm")
    database = tmp_path / "absent.sqlite3"
    assert recognition.identify_audio(path, database) == {"status": "no_match"}
    assert not database.exists()
    if suffix == ".webm":
        with pytest.raises(FingerprintFailure, match="duration"):
            fingerprint.decode_audio(path)


@pytest.mark.parametrize("seconds", [1, 13])
def test_query_requires_two_to_twelve_decoded_seconds(tmp_path, seconds):
    path = encode(generated_song(12, seconds), tmp_path / "query.webm", streaming=True)
    with pytest.raises(FingerprintFailure):
        recognition.identify_audio(path, tmp_path / "absent.sqlite3")


def test_incompatible_catalog_is_preserved_even_for_silence(tmp_path):
    database = tmp_path / "catalog.sqlite3"
    with catalog.open_catalog(database, initialize=True) as connection:
        connection.execute("UPDATE catalog_meta SET profile = '{}' WHERE id = 1")
        connection.commit()
    before = database.read_bytes()
    path = encode(np.zeros(10 * fingerprint.SAMPLE_RATE, dtype=np.float32), tmp_path / "silence.m4a")
    with pytest.raises(catalog.CatalogFailure, match="Rebuild"):
        recognition.identify_audio(path, database)
    assert database.read_bytes() == before


def test_held_out_generated_baseline_and_ambiguous_duplicate(connection, tmp_path):
    directory = tmp_path / "held-out"
    directory.mkdir()
    rows = measure_cases(directory, 2200)
    outcomes = [supported_match(row["candidates"]) for row in rows]
    assert sum(match is not None and match.track_id == row["expected"] for row, match in zip(rows, outcomes)) == 47
    assert all(match is None or match.track_id == row["expected"] for row, match in zip(rows, outcomes))
    for key, frame in fingerprint.fingerprint_samples(generated_song(12, 10)):
        posting(connection, key, frame)
    connection.execute("INSERT INTO tracks VALUES (?, ?, ?, ?, ?, ?)", ("track000001", "Duplicate", None, "https://example.test/duplicate", b"source", ".m4a"))
    connection.execute("INSERT INTO fingerprints SELECT hash_key, 'track000001', anchor_frame FROM fingerprints")
    assert supported_match(score_query(connection, fingerprint.fingerprint_samples(generated_song(12, 10)))) is None


@pytest.fixture
def query_client(monkeypatch, tmp_path):
    # Put request temporaries in a known directory so cleanup can be asserted on every path.
    temporary_root = tmp_path / "queries"
    temporary_root.mkdir()
    monkeypatch.setattr(api.tempfile, "tempdir", str(temporary_root))
    with TestClient(api.create_app(tmp_path / "imports", tmp_path / "catalog.sqlite3")) as client:
        yield client, temporary_root
    assert list(temporary_root.iterdir()) == []


@pytest.mark.parametrize("error,status", [(None, 200), (FingerprintFailure("Invalid clip"), 422),
    (FingerprintProcessingFailure("Decoder unavailable"), 503), (catalog.CatalogFailure("Rebuild catalog"), 409),
    (RuntimeError("internal secret"), 500)])
def test_api_worker_errors_and_cleanup(query_client, monkeypatch, error, status):
    client, root = query_client
    worker_threads = []
    paths = []
    def identify(path, catalog_path):
        paths.append(path)
        worker_threads.append(get_ident())
        assert path.read_bytes() == b"query audio"
        if error:
            raise error
        return {"status": "no_match"}
    monkeypatch.setattr(api, "identify_audio", identify)
    response = client.post("/api/identify", content=b"query audio", headers={"Content-Type": "audio/webm;codecs=opus"})
    assert response.status_code == status
    assert "internal secret" not in response.text
    assert list(root.iterdir()) == [] and not paths[0].exists()
    assert worker_threads[0] != get_ident()


@pytest.mark.parametrize("content,media_type,status", [(b"", "audio/webm", 422),
    (b"bad", "audio/mpeg", 415), (b"bad", "audio/webm", 422),
    (b"x" * (recognition.MAX_QUERY_BYTES + 1), "audio/webm", 413)])
def test_api_rejects_upload_and_cleans_files(query_client, content, media_type, status):
    client, root = query_client
    response = client.post("/api/identify", content=content, headers={"Content-Type": media_type})
    assert response.status_code == status
    assert list(root.iterdir()) == []


def test_real_api_match_returns_only_metadata_and_discards_clip(query_client, tmp_path):
    client, root = query_client
    source = encode(generated_song(1200), tmp_path / "source.m4a")
    with catalog.open_catalog(tmp_path / "catalog.sqlite3", initialize=True) as connection:
        catalog.index_track(connection, catalog.TrackResult("track000000", "Fixture song", "Generated artist", None), source)
    start = int(5.333 * fingerprint.SAMPLE_RATE)
    clip = encode(generated_song(1200)[start:start + 10 * fingerprint.SAMPLE_RATE], tmp_path / "clip.webm", streaming=True)
    response = client.post("/api/identify", content=clip.read_bytes(), headers={"Content-Type": "audio/webm;codecs=opus"})
    assert response.json() == {"status": "match", "track": {"video_id": "track000000", "title": "Fixture song",
        "channel": "Generated artist", "source_url": "https://www.youtube.com/watch?v=track000000"}}
    assert list(root.iterdir()) == []
    with sqlite3.connect(tmp_path / "catalog.sqlite3") as connection:
        assert connection.execute("SELECT count(*) FROM tracks").fetchone()[0] == 1


@pytest.mark.parametrize("seconds", [2, 12])
def test_exact_decoded_query_duration_boundaries(tmp_path, seconds):
    path = encode(generated_song(12, seconds), tmp_path / "query.webm", streaming=True)
    assert recognition.identify_audio(path, tmp_path / "absent.sqlite3") == {"status": "no_match"}


def test_decoder_unavailable_and_timeout_are_processing_errors(tmp_path, monkeypatch):
    path = tmp_path / "query.webm"
    path.write_bytes(b"nonempty")
    for error in (FileNotFoundError(), subprocess.TimeoutExpired("ffprobe", 30)):
        def fail(*args, **kwargs):
            raise error
        monkeypatch.setattr(fingerprint.subprocess, "run", fail)
        with pytest.raises(FingerprintProcessingFailure):
            recognition.identify_audio(path, tmp_path / "absent.sqlite3")


def test_interrupted_upload_cleans_temporary_audio(tmp_path, monkeypatch):
    import asyncio
    from starlette.requests import Request
    from fastapi import HTTPException
    monkeypatch.setattr(api.tempfile, "tempdir", str(tmp_path))
    app = api.create_app(tmp_path / "imports", tmp_path / "catalog.sqlite3")
    endpoint = next(route.endpoint for route in app.routes if route.path == "/api/identify")
    messages = iter([{"type": "http.request", "body": b"partial", "more_body": True}, {"type": "http.disconnect"}])
    async def receive():
        return next(messages)
    request = Request({"type": "http", "headers": [(b"content-type", b"audio/webm")]}, receive)
    with pytest.raises(HTTPException) as error:
        asyncio.run(endpoint(request))
    assert error.value.status_code == 400
    assert list(tmp_path.iterdir()) == []


def test_cancelled_request_waits_for_worker_before_removing_audio(tmp_path, monkeypatch):
    import asyncio
    from starlette.requests import Request
    monkeypatch.setattr(api.tempfile, "tempdir", str(tmp_path))
    started, release = Event(), Event()
    paths = []
    def identify(path, catalog_path):
        paths.append(path)
        started.set()
        assert release.wait(5)
        assert path.read_bytes() == b"query audio"
        return {"status": "no_match"}
    monkeypatch.setattr(api, "identify_audio", identify)
    app = api.create_app(tmp_path / "imports", tmp_path / "catalog.sqlite3")
    endpoint = next(route.endpoint for route in app.routes if route.path == "/api/identify")
    async def receive():
        return {"type": "http.request", "body": b"query audio", "more_body": False}
    request = Request({"type": "http", "headers": [(b"content-type", b"audio/webm")]}, receive)
    async def run():
        task = asyncio.create_task(endpoint(request))
        try:
            assert await asyncio.to_thread(started.wait, 5)
            task.cancel()
            await asyncio.sleep(0)
            assert paths[0].exists()
            task.cancel()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(run())
    assert list(tmp_path.iterdir()) == []
