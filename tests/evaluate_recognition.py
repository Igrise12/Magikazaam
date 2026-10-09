"""Offline labeled-audio evaluation; see docs/evaluation.md for the manifest and command."""

import argparse
from contextlib import closing
from dataclasses import asdict
import hashlib
import json
import math
from pathlib import Path
import sqlite3
from statistics import median
import tempfile
from time import perf_counter

from calibrate_recognition import accepted, select_thresholds
from magikazaam.catalog import CatalogFailure, open_catalog
from magikazaam.fingerprint import FingerprintFailure, SAMPLE_RATE, decode_audio, fingerprint_samples
from magikazaam.recognition import (
    MAX_QUERY_BYTES, MAX_QUERY_SECONDS, MIN_QUERY_SECONDS,
    MIN_ALIGNED_VOTES, MIN_SUPPORT_RATIO, MIN_SUPPORT_GAP, score_query, supported_match,
)

BASELINE = (MIN_ALIGNED_VOTES, MIN_SUPPORT_RATIO, MIN_SUPPORT_GAP)


def load_manifest(path: Path, track_ids: set[str]) -> tuple[list[dict], str]:
    """Validate labels, paths and song-level splits before any measurements."""
    raw = path.read_bytes()
    entries = json.loads(raw)
    if not isinstance(entries, list) or not entries:
        raise ValueError("Manifest must be a nonempty JSON array.")
    cases, paths, splits = set(), set(), {}
    rows = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("Each manifest entry must be an object.")
        for field in ("case", "path", "condition", "source_group", "split"):
            if not isinstance(entry.get(field), str) or not entry[field].strip():
                raise ValueError(f"Each entry needs a nonempty {field} string.")
        if "expected" not in entry or (entry["expected"] is not None
                                       and (not isinstance(entry["expected"], str) or entry["expected"] not in track_ids)):
            raise ValueError("expected must be null or an indexed track ID.")
        if entry["split"] not in {"calibration", "held_out"}:
            raise ValueError("split must be calibration or held_out.")
        audio = path.parent / entry["path"]
        if audio.is_symlink() or not audio.is_file() or audio.suffix not in {".m4a", ".webm"}:
            raise ValueError("Clip paths must name existing M4A/AAC or WebM/Opus files, not symlinks.")
        audio = audio.resolve()
        if entry["case"] in cases or audio in paths:
            raise ValueError("Case names and clip paths must be unique.")
        cases.add(entry["case"])
        paths.add(audio)
        # Check track labels too, so renaming a group cannot leak an indexed song.
        keys = [("group", entry["source_group"])]
        if entry["expected"] is not None:
            keys.append(("track", entry["expected"]))
        with audio.open("rb") as input_audio:
            digest = hashlib.file_digest(input_audio, "sha256").hexdigest()
        keys.append(("audio", digest))
        for key in keys:
            if key in splits and splits[key] != entry["split"]:
                raise ValueError("Source songs, indexed tracks and identical audio cannot cross splits.")
            splits[key] = entry["split"]
        rows.append({**entry, "path": audio, "audio_sha256": digest})
    for split in ("calibration", "held_out"):
        selected = [row for row in rows if row["split"] == split]
        if not any(row["expected"] is not None for row in selected) or not any(row["expected"] is None for row in selected):
            raise ValueError("Each split needs positive and negative/no-match clips.")
    return rows, hashlib.sha256(raw).hexdigest()


def measure_clip(connection: sqlite3.Connection, row: dict) -> dict:
    """Time the bounded backend audio pipeline, preserving errors as separate outcomes."""
    started = perf_counter()
    candidates, error, baseline_match = [], None, None
    try:
        if not 0 < row["path"].stat().st_size <= MAX_QUERY_BYTES:
            raise FingerprintFailure("Recording must be nonempty and at most 5 MiB.")
        samples = decode_audio(row["path"], max_duration=MAX_QUERY_SECONDS, allow_missing_duration=True)
        if samples.size < MIN_QUERY_SECONDS * SAMPLE_RATE:
            raise FingerprintFailure("Record at least 2 seconds of audio.")
        candidates = score_query(connection, fingerprint_samples(samples))
        match = supported_match(candidates)
        baseline_match = match.track_id if match is not None else None
    except FingerprintFailure as exc:
        error = str(exc)
    except OSError:
        error = "Recording could not be read. Check file permissions and retry."
    return {**row, "candidates": candidates, "error": error,
            "baseline_match": baseline_match, "processing_seconds": perf_counter() - started}


def metrics(rows: list[dict], thresholds: tuple[int, float, float]) -> dict:
    """Errors stay in rate denominators and are never counted as rejected negatives."""
    positives = sum(row["expected"] is not None for row in rows)
    negatives = len(rows) - positives
    correct = wrong = false_positives = misses = errors = 0
    durations = []
    for row in rows:
        if row["error"]:
            errors += 1
            continue
        durations.append(row["processing_seconds"])
        outcome = accepted(row, *thresholds)
        if row["expected"] is not None:
            correct += outcome == row["expected"]
            wrong += outcome is not None and outcome != row["expected"]
            misses += outcome is None
        else:
            false_positives += outcome is not None
    durations.sort()
    return {"clips": len(rows), "positives": positives, "negatives": negatives,
            "correct_positives": correct, "correct_identification_rate": correct / positives if positives else None,
            "false_positives": false_positives, "negative_false_positive_rate": false_positives / negatives if negatives else None,
            "wrong_song_matches": wrong, "incorrect_matches": wrong + false_positives,
            "misses": misses, "processing_errors": errors,
            "processing_seconds": {"count": len(durations), "median": median(durations) if durations else None,
                                   "p95": durations[math.ceil(0.95 * len(durations)) - 1] if durations else None}}


def summaries(rows: list[dict], thresholds: tuple[int, float, float]) -> dict:
    return {"overall": metrics(rows, thresholds),
            "by_condition": {condition: metrics([row for row in rows if row["condition"] == condition], thresholds)
                             for condition in sorted({row["condition"] for row in rows})}}


def compare(rows: list[dict], tuned: tuple[int, float, float] | None) -> dict:
    """Gate the already-frozen candidate; never search thresholds on held-out rows."""
    baseline, candidate = {}, {}
    for split in ("calibration", "held_out"):
        selected = [row for row in rows if row["split"] == split]
        baseline[split] = summaries(selected, BASELINE)
        candidate[split] = summaries(selected, tuned) if tuned else None
    reasons = []
    if tuned is None:
        reasons.append("No useful thresholds separate calibration positives from incorrect matches.")
    else:
        if candidate["calibration"]["overall"]["correct_positives"] <= baseline["calibration"]["overall"]["correct_positives"]:
            reasons.append("Calibration recognition did not improve.")
        if any(candidate[split]["overall"]["incorrect_matches"] for split in candidate):
            reasons.append("Candidate produced incorrect matches.")
        if candidate["held_out"]["overall"]["correct_positives"] < baseline["held_out"]["overall"]["correct_positives"]:
            reasons.append("Held-out recognition regressed.")
        if any(row["error"] for row in rows):
            reasons.append("Processing errors must be resolved before adopting thresholds.")
    return {"baseline": baseline, "candidate": candidate,
            "adoption": {"eligible": not reasons, "reasons": reasons,
                         "recommended_thresholds": threshold_dict(tuned if not reasons else BASELINE)}}


def threshold_dict(values: tuple[int, float, float]) -> dict:
    return dict(zip(("min_aligned_votes", "min_support_ratio", "min_support_gap"), values))


def evaluate(catalog: Path, manifest: Path) -> dict:
    """Evaluate one consistent backup snapshot, then discard it and all query hashes."""
    with tempfile.TemporaryDirectory(prefix="magikazaam-evaluation-") as temporary:
        snapshot = Path(temporary) / "catalog.sqlite3"
        with open_catalog(catalog) as source:
            with closing(sqlite3.connect(snapshot)) as destination:
                started = perf_counter()
                source.backup(destination)
                backup_seconds = perf_counter() - started
                if destination.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                    raise ValueError("Temporary catalog backup failed integrity verification.")
                if destination.execute("PRAGMA foreign_key_check").fetchone() is not None:
                    raise ValueError("Temporary catalog backup has broken track references.")
        with open_catalog(snapshot) as connection:
            track_ids = {row[0] for row in connection.execute("SELECT video_id FROM tracks")}
            profile = json.loads(connection.execute("SELECT profile FROM catalog_meta WHERE id = 1").fetchone()[0])
            entries, manifest_digest = load_manifest(manifest, track_ids)
            calibration = [measure_clip(connection, row) for row in entries if row["split"] == "calibration"]
            tuned = select_thresholds(calibration)
            held_out = [measure_clip(connection, row) for row in entries if row["split"] == "held_out"]
        rows = calibration + held_out
        report = compare(rows, tuned)
        report.update({"catalog": {"track_count": len(track_ids), "profile": profile,
                                   "database_bytes": catalog.stat().st_size, "snapshot_bytes": snapshot.stat().st_size,
                                   "backup_seconds": backup_seconds, "backup_integrity": "ok"},
                       "dataset": {"manifest_sha256": manifest_digest, "clip_count": len(rows)},
                       "thresholds": {"baseline": threshold_dict(BASELINE), "candidate": threshold_dict(tuned) if tuned else None},
                       "timing_scope": "Per-clip validation, decode, fingerprinting, lookup and baseline decision; excludes upload, startup and metadata serialization. Successful clips only; p95 uses nearest rank.",
                       "clips": [{key: value for key, value in row.items() if key not in {"path", "candidates"}} |
                                 {"candidates": [asdict(candidate) for candidate in row["candidates"][:2]],
                                  "candidate_match": accepted(row, *tuned) if tuned and not row["error"] else None}
                                 for row in rows]})
        return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        # Never overwrite a catalog, manifest, recording or previous report.
        if args.output.exists():
            raise ValueError("Report destination already exists; choose a new file.")
        report = evaluate(args.catalog, args.manifest)
        with args.output.open("x", encoding="utf-8") as output:
            output.write(json.dumps(report, indent=2, allow_nan=False) + "\n")
    except (CatalogFailure, ValueError, OSError) as exc:
        parser.exit(1, f"Evaluation failed: {exc}\n")
    print(f"Report written to {args.output}. Threshold adoption eligible: {report['adoption']['eligible']}")


if __name__ == "__main__":
    main()
