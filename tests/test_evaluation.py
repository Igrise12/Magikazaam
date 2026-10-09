import json
from pathlib import Path
import shutil
import subprocess
import sys

import numpy as np
import pytest

import evaluate_recognition as evaluation
from calibrate_recognition import accepted, select_thresholds
from magikazaam.catalog import index_track, open_catalog
from magikazaam.fingerprint import SAMPLE_RATE, FingerprintFailure
from magikazaam.importer import TrackResult
from magikazaam.recognition import Candidate, supported_match
from recognition_cases import encode, generated_song


def scored(expected, votes=20, support=0.1, runner=0, **fields):
    candidates = [Candidate("track000000", votes, support, 10)]
    if runner:
        candidates.append(Candidate("track000001", votes, runner, 20))
    return {"expected": expected, "candidates": candidates, "error": None,
            "processing_seconds": 1.0, "condition": "clean", **fields}


@pytest.fixture
def manifest(tmp_path):
    entries = []
    for index, (split, expected) in enumerate((
        ("calibration", "track000000"), ("calibration", None),
        ("held_out", "track000001"), ("held_out", None),
    )):
        path = tmp_path / f"clip-{index}.m4a"
        path.write_bytes(f"unique-content-{index}".encode())
        entries.append({"case": f"case-{index}", "path": path.name, "expected": expected,
                        "condition": "microphone/quiet", "source_group": f"song-{index}", "split": split})
    return tmp_path / "manifest.json", entries


def load(manifest):
    path, entries = manifest
    path.write_text(json.dumps(entries))
    return evaluation.load_manifest(path, {"track000000", "track000001"})


def test_manifest_resolves_paths_and_records_dataset_and_audio_checksums(manifest):
    rows, digest = load(manifest)
    assert rows[0]["path"] == manifest[0].parent / "clip-0.m4a"
    assert len(digest) == len(rows[0]["audio_sha256"]) == 64
    manifest[1][0]["condition"] = "microphone/noise"
    assert load(manifest)[1] != digest


@pytest.mark.parametrize("field,value", [
    ("expected", "unknown"), ("expected", []), ("case", ""), ("condition", 1),
    ("source_group", None), ("split", "test"), ("path", "missing.m4a"),
])
def test_invalid_manifest_fields(manifest, field, value):
    manifest[1][0][field] = value
    with pytest.raises(ValueError):
        load(manifest)


@pytest.mark.parametrize("value", [[], {}, [None], [True]])
def test_invalid_manifest_shape(tmp_path, value):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError):
        evaluation.load_manifest(path, set())


@pytest.mark.parametrize("leak", ["source_group", "expected", "content"])
def test_manifest_rejects_cross_split_leakage(manifest, leak):
    entries = manifest[1]
    if leak == "content":
        root = manifest[0].parent
        (root / entries[2]["path"]).write_bytes((root / entries[0]["path"]).read_bytes())
    else:
        entries[2][leak] = entries[0][leak]
    with pytest.raises(ValueError, match="cross splits"):
        load(manifest)


@pytest.mark.parametrize("field", ["case", "path"])
def test_manifest_rejects_duplicate_cases_and_paths(manifest, field):
    manifest[1][1][field] = manifest[1][0][field]
    with pytest.raises(ValueError, match="unique"):
        load(manifest)


def test_manifest_requires_explicit_labels_and_both_classes_in_each_split(manifest):
    entries = manifest[1]
    del entries[0]["expected"]
    with pytest.raises(ValueError, match="expected"):
        load(manifest)
    entries[0]["expected"] = "track000000"
    entries.pop()
    with pytest.raises(ValueError, match="positive and negative"):
        load(manifest)


def test_symlink_clip_is_rejected(manifest):
    path, entries = manifest
    (path.parent / "linked.m4a").symlink_to(path.parent / entries[0]["path"])
    entries[0]["path"] = "linked.m4a"
    with pytest.raises(ValueError, match="symlinks"):
        load(manifest)


def test_metrics_count_errors_misses_wrong_songs_and_false_positives_separately():
    rows = [scored("track000000"), scored("track000001"), scored(None),
            scored("track000000", candidates=[]), scored(None, error="Decoder failed"),
            scored("track000000", error="Decoder failed")]
    result = evaluation.metrics(rows, (1, 0.01, 0.01))
    assert result["correct_identification_rate"] == 1 / 4
    assert result["negative_false_positive_rate"] == 1 / 2
    assert result["wrong_song_matches"] == result["false_positives"] == result["misses"] == 1
    assert result["processing_errors"] == result["incorrect_matches"] == 2
    assert result["processing_seconds"] == {"count": 4, "median": 1.0, "p95": 1.0}
    assert evaluation.metrics([], evaluation.BASELINE)["correct_identification_rate"] is None
    rows[0]["condition"] = "noise"
    assert evaluation.summaries(rows, (1, 0.01, 0.01))["by_condition"]["noise"]["clips"] == 1


def test_p95_uses_nearest_rank():
    rows = [scored(None, candidates=[], processing_seconds=value) for value in range(1, 21)]
    assert evaluation.metrics(rows, evaluation.BASELINE)["processing_seconds"] == {
        "count": 20, "median": 10.5, "p95": 19,
    }


def test_baseline_acceptance_matches_production():
    for row in (scored(None), scored(None, votes=15), scored(None, support=0.001),
                scored(None, runner=0.1), scored(None, candidates=[])):
        result = supported_match(row["candidates"])
        assert accepted(row, *evaluation.BASELINE) == (result.track_id if result else None)


def test_calibration_handles_single_candidate_gaps_and_rejects_wrong_songs():
    rows = [scored("track000000", votes=10, support=0.05), scored(None, votes=9, support=0.04),
            scored("track000001", votes=50, support=0.05, runner=0.049)]
    thresholds = select_thresholds(rows)
    assert thresholds == (10, 0.05, 0.05)
    assert [accepted(row, *thresholds) for row in rows] == ["track000000", None, None]
    assert select_thresholds(list(reversed(rows))) == thresholds


@pytest.mark.parametrize("rows", [[], [scored(None)], [scored("track000000", candidates=[])],
                                  [scored("track000000", runner=0.1)],
                                  [scored("track000000", error="failed")],
                                  [scored("track000000"), scored(None)]])
def test_no_useful_safe_thresholds(rows):
    assert select_thresholds(rows) is None


def gate_rows():
    return [scored("track000000", votes=10, split="calibration"),
            scored(None, candidates=[], split="calibration"),
            scored("track000000", votes=20, split="held_out"),
            scored(None, candidates=[], split="held_out")]


def test_adoption_requires_improvement_no_errors_and_preserved_held_out_results():
    rows = gate_rows()
    assert evaluation.compare(rows, (10, 0.1, 0.1))["adoption"]["eligible"]
    assert not evaluation.compare(rows, evaluation.BASELINE)["adoption"]["eligible"]
    assert not evaluation.compare(rows, None)["adoption"]["eligible"]
    assert not evaluation.compare(rows, (10, 0.2, 0.1))["adoption"]["eligible"]
    rows[3] = scored(None, split="held_out")
    assert "Candidate produced incorrect matches." in evaluation.compare(rows, (10, 0.1, 0.1))["adoption"]["reasons"]
    rows[3] = scored(None, error="Decoder failed", split="held_out")
    assert not evaluation.compare(rows, (10, 0.1, 0.1))["adoption"]["eligible"]


def test_clip_errors_are_reported_and_do_not_become_no_matches(tmp_path, monkeypatch):
    path = tmp_path / "clip.m4a"
    path.write_bytes(b"audio")
    monkeypatch.setattr(evaluation, "decode_audio", lambda *args, **kwargs: np.zeros(SAMPLE_RATE))
    row = evaluation.measure_clip(None, {"path": path})
    assert "2 seconds" in row["error"]
    assert row["candidates"] == []
    monkeypatch.setattr(evaluation, "MAX_QUERY_BYTES", 2)
    assert "5 MiB" in evaluation.measure_clip(None, {"path": path})["error"]
    def failed(*args, **kwargs):
        raise FingerprintFailure("Bad encoding")
    monkeypatch.setattr(evaluation, "MAX_QUERY_BYTES", 100)
    monkeypatch.setattr(evaluation, "decode_audio", failed)
    assert evaluation.measure_clip(None, {"path": path})["error"] == "Bad encoding"
    path.unlink()
    row = evaluation.measure_clip(None, {"path": path})
    assert "could not be read" in row["error"]
    assert str(path) not in row["error"]


@pytest.fixture
def real_dataset(tmp_path):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("Real audio evaluation requires FFmpeg/ffprobe")
    catalog = tmp_path / "catalog.sqlite3"
    originals = [generated_song(1200), generated_song(2200)]
    with open_catalog(catalog, initialize=True) as connection:
        for index, original in enumerate(originals):
            index_track(connection, TrackResult(f"track{index:06d}", f"Song {index}", None, None),
                        encode(original, tmp_path / f"source-{index}.m4a"))
        duplicate = generated_song(3200)
        audio = encode(duplicate, tmp_path / "duplicate.m4a")
        for video_id in ("duplicate00", "duplicate01"):
            index_track(connection, TrackResult(video_id, "Duplicate", None, None), audio)
    entries = []
    for index, split in enumerate(("calibration", "held_out")):
        for offset in (0.1137, 5.333, 12.731):
            for condition in ("clean", "quiet", "noise", "opus"):
                start = int(offset * SAMPLE_RATE)
                clip = originals[index][start:start + 10 * SAMPLE_RATE].copy()
                if condition == "quiet":
                    clip *= 0.2
                if condition == "noise":
                    clip += np.random.default_rng(index).normal(0, 0.01, clip.size).astype(np.float32)
                name = f"{split}-{offset}-{condition}"
                path = tmp_path / (name + (".webm" if condition == "opus" else ".m4a"))
                encode(clip, path, streaming=condition == "opus")
                entries.append({"case": name, "path": path.name, "expected": f"track{index:06d}",
                                "condition": condition, "source_group": f"song-{index}", "split": split})
        negative = encode(generated_song(9000 + index, 10), tmp_path / f"negative-{index}.webm", streaming=True)
        entries.append({"case": f"negative-{index}", "path": negative.name, "expected": None,
                        "condition": "outside_catalog", "source_group": f"negative-{index}", "split": split})
    silence = encode(np.zeros(10 * SAMPLE_RATE, dtype=np.float32), tmp_path / "silence.m4a")
    ambiguous = encode(duplicate[:10 * SAMPLE_RATE], tmp_path / "ambiguous.webm", streaming=True)
    for path, condition, split in ((silence, "silence", "calibration"), (ambiguous, "ambiguous", "held_out")):
        entries.append({"case": condition, "path": path.name, "expected": None, "condition": condition,
                        "source_group": condition, "split": split})
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(entries))
    return catalog, manifest


def test_real_runner_and_cli_preserve_catalog_and_clips_and_remove_backup(real_dataset, tmp_path, monkeypatch):
    catalog, manifest = real_dataset
    temporary = tmp_path / "temporary"
    temporary.mkdir()
    monkeypatch.setattr(evaluation.tempfile, "tempdir", str(temporary))
    before = {path.name: path.read_bytes() for path in tmp_path.iterdir() if path.is_file()}
    select = evaluation.select_thresholds
    def calibration_only(rows):
        assert len(rows) == 14
        assert {row["split"] for row in rows} == {"calibration"}
        return select(rows)
    monkeypatch.setattr(evaluation, "select_thresholds", calibration_only)
    report = evaluation.evaluate(catalog, manifest)
    assert report["catalog"]["track_count"] == 4
    assert report["catalog"]["backup_integrity"] == "ok"
    assert report["catalog"]["database_bytes"] == len(before[catalog.name])
    assert report["catalog"]["backup_seconds"] > 0
    assert report["dataset"]["clip_count"] == 28
    assert report["baseline"]["calibration"]["overall"]["correct_positives"] > 0
    assert report["baseline"]["held_out"]["overall"]["correct_positives"] > 0
    assert all(report["baseline"][split]["overall"]["incorrect_matches"] == 0 for split in ("calibration", "held_out"))
    assert not report["adoption"]["eligible"]
    assert "path" not in report["clips"][0]
    assert all(len(row["candidates"]) <= 2 for row in report["clips"])
    assert list(temporary.iterdir()) == []
    assert before == {path.name: path.read_bytes() for path in tmp_path.iterdir() if path.is_file()}
    output = tmp_path / "report.json"
    monkeypatch.setattr(sys, "argv", ["evaluate", "--catalog", str(catalog), "--manifest", str(manifest), "--output", str(output)])
    evaluation.main()
    assert json.loads(output.read_text())["dataset"] == report["dataset"]
    with pytest.raises(SystemExit) as failure:
        evaluation.main()
    assert failure.value.code == 1
    assert list(temporary.iterdir()) == []


def test_backup_is_removed_when_manifest_validation_fails(manifest, tmp_path, monkeypatch):
    catalog = tmp_path / "catalog.sqlite3"
    with open_catalog(catalog, initialize=True):
        pass
    path = manifest[0]
    path.write_text("[]")
    temporary = tmp_path / "temporary"
    temporary.mkdir()
    monkeypatch.setattr(evaluation.tempfile, "tempdir", str(temporary))
    with pytest.raises(ValueError):
        evaluation.evaluate(catalog, path)
    assert list(temporary.iterdir()) == []


def test_backup_integrity_failure_removes_snapshot(tmp_path, monkeypatch):
    catalog = tmp_path / "catalog.sqlite3"
    with open_catalog(catalog, initialize=True) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("INSERT INTO fingerprints VALUES (1, 'missing', 0)")
        connection.commit()
    temporary = tmp_path / "temporary"
    temporary.mkdir()
    monkeypatch.setattr(evaluation.tempfile, "tempdir", str(temporary))
    with pytest.raises(ValueError, match="broken track references"):
        evaluation.evaluate(catalog, tmp_path / "unused.json")
    assert list(temporary.iterdir()) == []


def test_cli_rejects_missing_catalog_without_creating_it(tmp_path):
    command = [sys.executable, str(Path(evaluation.__file__)), "--catalog", str(tmp_path / "missing.sqlite3"),
               "--manifest", str(tmp_path / "missing.json"), "--output", str(tmp_path / "report.json")]
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode == 1
    assert "Evaluation failed" in result.stderr
    assert list(tmp_path.iterdir()) == []
