import json
import sqlite3
from threading import Event
from unittest.mock import Mock

from fastapi.testclient import TestClient
import pytest

from magikazaam import api, catalog, cli
from magikazaam.importer import ImportFailure, TrackResult


PLAYLIST_ID = "PLabcdefghijklmno"


def test_api_shared_importer_progress_conflict_and_completion(monkeypatch, tmp_path):
    started, release, finished = Event(), Event(), Event()

    def importer(result, staging_root, on_progress, catalog_path):
        assert catalog_path == tmp_path / "catalog.sqlite3"
        assert staging_root == tmp_path
        result.state, result.total = "running", 1
        on_progress(result)
        started.set()
        assert release.wait(5)
        result.tracks = [TrackResult("abcdefghijk", "Prepared song", "Artist", "https://www.youtube.com/watch?v=abcdefghijk",
                                    status="indexed")]
        result.completed, result.state = 1, "completed"
        on_progress(result)
        finished.set()
        return result

    shared_importer = Mock(side_effect=importer)
    monkeypatch.setattr(api, "import_playlist", shared_importer)
    with TestClient(api.create_app(tmp_path, tmp_path / "catalog.sqlite3")) as client:
        assert client.get("/api/health").json() == {"status": "ok"}
        response = client.post("/api/imports", json={"playlist": PLAYLIST_ID})
        assert response.status_code == 202
        import_id = response.json()["import_id"]
        try:
            assert started.wait(5)
            status = client.get(f"/api/imports/{import_id}").json()
            assert status["state"] == "running" and status["total"] == 1
            assert client.post("/api/imports", json={"playlist": PLAYLIST_ID}).status_code == 409
        finally:
            release.set()
        assert finished.wait(5)
        completed = client.get(f"/api/imports/{import_id}").json()
        assert completed["state"] == "completed" and completed["completed"] == 1
        assert "audio_file" not in completed["tracks"][0]
        assert client.post("/api/imports", json={"playlist": PLAYLIST_ID}).status_code == 202
    with TestClient(api.create_app(tmp_path, tmp_path / "catalog.sqlite3")) as restarted:
        assert restarted.get(f"/api/imports/{import_id}").status_code == 404
    assert shared_importer.call_count == 2


@pytest.mark.parametrize("body", [{"playlist": "https://example.com"}, {"playlist": ""}, {}, {"playlist": "x" * 2049}])
def test_api_validates_request_before_starting_import(body, tmp_path):
    with TestClient(api.create_app(tmp_path, tmp_path / "catalog.sqlite3")) as client:
        assert client.post("/api/imports", json=body).status_code == 422
        assert client.get("/api/imports/unknown").status_code == 404
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("error", [ImportFailure("Install node before importing."), RuntimeError("internal secret")])
def test_api_background_failure_is_terminal(monkeypatch, tmp_path, error):
    monkeypatch.setattr(api, "import_playlist", Mock(side_effect=error))
    app = api.create_app(tmp_path, tmp_path / "catalog.sqlite3")
    with TestClient(app) as client:
        import_id = client.post("/api/imports", json={"playlist": PLAYLIST_ID}).json()["import_id"]
    with TestClient(app) as client:
        status = client.get(f"/api/imports/{import_id}").json()
    assert status["state"] == "failed"
    assert "internal secret" not in status["error"]


def test_cli_calls_shared_importer(monkeypatch, capsys):
    def importer(result):
        result.state, result.total, result.completed = "completed", 1, 1
        result.tracks = [TrackResult("abcdefghijk", "Song", None, None, status="indexed")]
        return result
    shared = Mock(side_effect=importer)
    monkeypatch.setattr(cli, "import_playlist", shared)
    monkeypatch.setattr("sys.argv", ["magikazaam", "import-playlist", PLAYLIST_ID])
    with pytest.raises(SystemExit) as exit:
        cli.main()
    assert exit.value.code == 0
    assert json.loads(capsys.readouterr().out)["tracks"][0]["status"] == "indexed"
    assert shared.call_args.args[0].playlist == f"https://www.youtube.com/playlist?list={PLAYLIST_ID}"


def test_cli_returns_failure_for_partial_import(monkeypatch, capsys):
    def importer(result):
        result.state = "completed"
        result.tracks = [TrackResult(None, "Unavailable", None, None, error="Unavailable")]
        return result
    monkeypatch.setattr(cli, "import_playlist", importer)
    monkeypatch.setattr("sys.argv", ["magikazaam", "import-playlist", PLAYLIST_ID])
    with pytest.raises(SystemExit) as exit:
        cli.main()
    assert exit.value.code == 1
    assert json.loads(capsys.readouterr().out)["tracks"][0]["status"] == "failed"


def test_cli_starts_loopback_api(monkeypatch):
    import uvicorn
    server = Mock()
    monkeypatch.setattr(uvicorn, "run", server)
    monkeypatch.setattr("sys.argv", ["magikazaam"])
    cli.main()
    server.assert_called_once_with("magikazaam.api:app", host="127.0.0.1", port=8001, workers=1)


def test_catalog_api_empty_and_persistent_after_restart(tmp_path, monkeypatch):
    database = tmp_path / "catalog.sqlite3"
    with TestClient(api.create_app(tmp_path, database)) as client:
        assert client.get("/api/catalog").json() == []
        assert not database.exists()
    audio = tmp_path / "audio.m4a"
    audio.write_bytes(b"private audio bytes")
    monkeypatch.setattr(catalog, "fingerprint_audio", lambda path: iter([((1, 2, 3), 0)]))
    with catalog.open_catalog(database, initialize=True) as connection:
        catalog.index_track(connection, TrackResult("abcdefghijk", "Song", "Artist", None), audio)
    with TestClient(api.create_app(tmp_path, database)) as client:
        response = client.get("/api/catalog")
        assert response.status_code == 200
        assert response.json() == [{"video_id": "abcdefghijk", "title": "Song", "channel": "Artist",
                                    "source_url": "https://www.youtube.com/watch?v=abcdefghijk"}]
        assert "private audio" not in response.text


def test_catalog_api_incompatible_profile_is_actionable(tmp_path):
    database = tmp_path / "catalog.sqlite3"
    with catalog.open_catalog(database, initialize=True) as connection:
        with connection:
            connection.execute("DELETE FROM catalog_meta")
    with TestClient(api.create_app(tmp_path, database)) as client:
        response = client.get("/api/catalog")
        assert response.status_code == 409
        assert "Rebuild" in response.json()["detail"]
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM catalog_meta").fetchone()[0] == 0


def test_cli_skipped_tracks_are_success(monkeypatch, capsys):
    def importer(result):
        result.state = "completed"
        result.tracks = [TrackResult("abcdefghijk", "Song", None, None, status="skipped")]
        return result
    monkeypatch.setattr(cli, "import_playlist", importer)
    monkeypatch.setattr("sys.argv", ["magikazaam", "import-playlist", PLAYLIST_ID])
    with pytest.raises(SystemExit) as exit:
        cli.main()
    assert exit.value.code == 0
    assert json.loads(capsys.readouterr().out)["tracks"][0]["status"] == "skipped"
