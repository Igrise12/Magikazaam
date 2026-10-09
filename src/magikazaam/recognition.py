"""Temporary microphone queries against the existing landmark catalog."""

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
import sqlite3

from magikazaam.catalog import CATALOG_PATH, check_profile, encode_key, open_catalog
from magikazaam.fingerprint import Fingerprint, FingerprintFailure, SAMPLE_RATE, decode_audio, fingerprint_samples


MAX_QUERY_BYTES = 5 * 1024 * 1024
MIN_QUERY_SECONDS = 2
MAX_QUERY_SECONDS = 12
# shortcut: synthetic-fixture baseline only; recalibrate with task 6's real microphone clips.
MIN_ALIGNED_VOTES = 16
MIN_SUPPORT_RATIO = 16 / 2303
MIN_SUPPORT_GAP = 14 / 2303


@dataclass(frozen=True)
class Candidate:
    track_id: str
    aligned_votes: int
    support_ratio: float
    offset_frame: int


def score_query(connection: sqlite3.Connection, fingerprints: Iterable[Fingerprint]) -> list[Candidate]:
    """Rank distinct query occurrences in each track's strongest ±1-frame cluster."""
    query = list(dict.fromkeys(fingerprints))
    if not query:
        return []
    by_key = defaultdict(list)
    for occurrence, (key, frame) in enumerate(query):
        by_key[encode_key(key)].append((occurrence, frame))
    votes = defaultdict(lambda: defaultdict(set))
    keys = list(by_key)
    for start in range(0, len(keys), 500):
        batch = keys[start:start + 500]
        placeholders = ",".join("?" for _ in batch)
        for key, track_id, catalog_frame in connection.execute(
            f"SELECT hash_key, track_id, anchor_frame FROM fingerprints WHERE hash_key IN ({placeholders})", batch
        ):
            for occurrence, query_frame in by_key[key]:
                votes[track_id][catalog_frame - query_frame].add(occurrence)
    candidates = []
    for track_id, offsets in votes.items():
        centers = {offset + shift for offset in offsets for shift in (-1, 0, 1)}
        best_count, best_center = max(
            (len(offsets.get(center - 1, set()) | offsets.get(center, set()) | offsets.get(center + 1, set())), -center)
            for center in centers
        )
        candidates.append(Candidate(track_id, best_count, best_count / len(query), -best_center))
    return sorted(candidates, key=lambda candidate: (-candidate.support_ratio, -candidate.aligned_votes, candidate.track_id))


def supported_match(candidates: list[Candidate]) -> Candidate | None:
    """Reject weak or ambiguous evidence; ties never pick a song."""
    if not candidates:
        return None
    best = candidates[0]
    runner_support = candidates[1].support_ratio if len(candidates) > 1 else 0
    if (best.aligned_votes < MIN_ALIGNED_VOTES or best.support_ratio < MIN_SUPPORT_RATIO
            or best.support_ratio - runner_support < MIN_SUPPORT_GAP):
        return None
    return best


def identify_audio(path: Path, catalog_path: Path = CATALOG_PATH) -> dict:
    """Validate a short recording and return metadata without persisting query data."""
    samples = decode_audio(path, max_duration=MAX_QUERY_SECONDS, allow_missing_duration=True)
    if samples.size < MIN_QUERY_SECONDS * SAMPLE_RATE:
        raise FingerprintFailure("Record at least 2 seconds of audio, then try again.")
    if not catalog_path.exists():
        return {"status": "no_match"}
    with open_catalog(catalog_path) as connection:
        connection.execute("BEGIN")
        check_profile(connection)
        candidates = score_query(connection, fingerprint_samples(samples))
        best = supported_match(candidates)
        if best is None:
            return {"status": "no_match"}
        track = connection.execute(
            "SELECT video_id, title, channel, source_url FROM tracks WHERE video_id = ?", (best.track_id,)
        ).fetchone()
        return {"status": "match", "track": dict(track)}
