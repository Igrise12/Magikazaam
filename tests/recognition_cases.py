"""Generated fixtures, never recordings from a user or YouTube."""

from pathlib import Path
import subprocess

import numpy as np

from magikazaam.catalog import index_track, open_catalog
from magikazaam.fingerprint import BAND_EDGES, SAMPLE_RATE, decode_audio, fingerprint_samples
from magikazaam.importer import TrackResult
from magikazaam.recognition import score_query


def generated_song(seed: int, seconds: int = 24) -> np.ndarray:
    rng = np.random.default_rng(seed)
    samples = np.zeros(seconds * SAMPLE_RATE, dtype=np.float32)
    length = SAMPLE_RATE // 2
    time = np.arange(length) / SAMPLE_RATE
    envelope = np.sin(np.pi * np.arange(length) / length) ** 2
    for start in range(0, samples.size, length):
        block = np.zeros(length)
        for low, high in zip(BAND_EDGES[:-1], BAND_EDGES[1:]):
            frequency = rng.integers(low, high) * 8
            block += rng.uniform(0.04, 0.12) * np.sin(2 * np.pi * frequency * time + rng.uniform(0, 2 * np.pi))
        samples[start:start + length] = block * envelope
    return samples


def encode(samples: np.ndarray, path: Path, *, streaming: bool = False) -> Path:
    codec = "libopus" if path.suffix == ".webm" else "aac"
    command = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "f32le", "-ar", str(SAMPLE_RATE),
               "-ac", "1", "-i", "pipe:0", "-c:a", codec]
    if streaming:
        result = subprocess.run(command + ["-f", "webm", "pipe:1"], input=samples.astype("<f4").tobytes(),
                                capture_output=True, check=True, timeout=30)
        path.write_bytes(result.stdout)
    else:
        subprocess.run(command + [str(path)], input=samples.astype("<f4").tobytes(),
                       capture_output=True, check=True, timeout=30)
    return path


def measure_cases(directory: Path, seed: int) -> list[dict]:
    database = directory / "catalog.sqlite3"
    originals = [generated_song(seed + index) for index in range(4)]
    rows = []
    with open_catalog(database, initialize=True) as connection:
        for index, samples in enumerate(originals):
            audio = encode(samples, directory / f"source-{index}.m4a")
            index_track(connection, TrackResult(f"track{index:06d}", f"Fixture song {index}", "Generated fixture", None), audio)
    with open_catalog(database) as connection:
        for index, original in enumerate(originals):
            for offset in (0.1137, 5.333, 12.731):
                start = int(offset * SAMPLE_RATE)
                clip = original[start:start + 10 * SAMPLE_RATE]
                for variant in ("clean", "quiet", "noise", "opus"):
                    query = clip.copy()
                    if variant == "quiet":
                        query *= 0.2
                    if variant == "noise":
                        query += np.random.default_rng(seed + index).normal(0, 0.01, query.size).astype(np.float32)
                    suffix = ".webm" if variant == "opus" else ".m4a"
                    path = encode(query, directory / f"query{suffix}", streaming=variant == "opus")
                    candidates = score_query(connection, fingerprint_samples(decode_audio(path, max_duration=12, allow_missing_duration=True)))
                    rows.append({"case": f"track-{index}/{offset}/{variant}", "expected": f"track{index:06d}",
                                 "candidates": candidates})
        for index in range(8):
            path = encode(generated_song(seed + 100 + index, 10), directory / "negative.webm", streaming=True)
            rows.append({"case": f"negative-{index}", "expected": None, "candidates": score_query(
                connection, fingerprint_samples(decode_audio(path, max_duration=12, allow_missing_duration=True)))})
        rows.append({"case": "silence", "expected": None, "candidates": score_query(
            connection, fingerprint_samples(np.zeros(10 * SAMPLE_RATE, dtype=np.float32)))})
    with open_catalog(database, initialize=True) as connection:
        index_track(connection, TrackResult("duplicate00", "Duplicate fixture", None, None), directory / "source-0.m4a")
    with open_catalog(database) as connection:
        clip = originals[0][int(5.333 * SAMPLE_RATE):int(5.333 * SAMPLE_RATE) + 10 * SAMPLE_RATE]
        path = encode(clip, directory / "ambiguous.webm", streaming=True)
        rows.append({"case": "ambiguous-duplicate", "expected": None, "candidates": score_query(
            connection, fingerprint_samples(decode_audio(path, max_duration=12, allow_missing_duration=True)))})
    return rows
