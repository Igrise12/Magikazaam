"""Shared, deterministic landmark fingerprints for catalog and query audio."""

from collections import deque
from collections.abc import Iterable, Iterator
import json
from pathlib import Path
import subprocess

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view
from numpy.typing import ArrayLike, NDArray
from scipy.fft import rfft
from scipy.ndimage import maximum_filter
from scipy.signal.windows import hamming

from magikazaam.importer import AudioProcessingFailure, ImportFailure, MAX_DURATION, validate_audio


SAMPLE_RATE = 8192
WINDOW_SIZE = 1024
HOP_SIZE = 32
BAND_EDGES = (1, 3, 8, 23, 64, 181, 513)
# shortcut: peak filter/density are provisional, calibrate with task 6's labeled recordings.
FILTER_FRAMES = 25
FILTER_BINS = 9
PEAK_INTERVAL = 25
TARGET_FRAMES = 50
BLOCK_FRAMES = 2500

Fingerprint = tuple[tuple[int, int, int], int]


class FingerprintFailure(ValueError):
    """Invalid audio, a decoding failure, or an incompatible fingerprint profile."""


class FingerprintProcessingFailure(FingerprintFailure):
    """Audio processing could not run or timed out."""


def fingerprint_profile() -> str:
    """Serialize every fingerprint setting; bump version when behavior changes."""
    return json.dumps({
        "version": 1,
        "audio": {"sample_rate": SAMPLE_RATE, "channels": 1, "pcm": "f32le",
                  "decoder": "ffmpeg", "downmix": "ffmpeg-default", "resampler": "swr"},
        "stft": {"window": "hamming-periodic", "window_samples": WINDOW_SIZE,
                 "hop_samples": HOP_SIZE, "spectrum": "rfft-magnitude", "padding": False},
        "peaks": {"band_edges": BAND_EDGES, "exclude_dc": True,
                  "maximum_filter": [FILTER_FRAMES, FILTER_BINS], "filter_padding": "constant-zero",
                  "interval_frames": PEAK_INTERVAL, "per_band_per_interval": 1,
                  "selection": "strongest-positive", "ties": "earliest-frame-lowest-bin"},
        "hash": {"target_frames": [1, TARGET_FRAMES], "pairing": "all-selected-peaks",
                 "encoding": "anchor-bin,target-bin,delta-frames", "bin_step": 1, "frame_step": 1},
    }, sort_keys=True, separators=(",", ":"))


def check_fingerprint_profile(stored_profile: str | None) -> None:
    """Reject missing, malformed, or different profiles before using an index."""
    try:
        stored = json.dumps(json.loads(stored_profile), sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        stored = None
    if stored != fingerprint_profile():
        raise FingerprintFailure("Fingerprint profile is missing or incompatible. Rebuild the catalog before matching.")


def decode_audio(path: Path, *, max_duration: float | None = None, allow_missing_duration: bool = False) -> NDArray[np.float32]:
    """Validate AAC/M4A or Opus/WebM and decode bounded mono PCM into memory."""
    path = path.absolute()
    limit = MAX_DURATION if max_duration is None else max_duration
    try:
        if max_duration is None and not allow_missing_duration:
            validate_audio(path)
        else:
            validate_audio(path, max_duration=limit, allow_missing_duration=allow_missing_duration)
        decoded = subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "error", "-xerror", "-protocol_whitelist", "file,pipe",
             "-i", str(path), "-map", "0:a:0", "-t", str(limit + 1),
             "-ac", "1", "-af", f"aresample={SAMPLE_RATE}:resampler=swr",
             "-ar", str(SAMPLE_RATE), "-c:a", "pcm_f32le", "-f", "f32le", "pipe:1"],
            capture_output=True, check=True, timeout=120,
        )
        samples = np.frombuffer(decoded.stdout, dtype="<f4")
    except AudioProcessingFailure as exc:
        raise FingerprintProcessingFailure(str(exc)) from exc
    except ImportFailure as exc:
        raise FingerprintFailure(str(exc)) from exc
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise FingerprintProcessingFailure("Audio could not be decoded. Check FFmpeg/ffprobe and file permissions, then retry.") from exc
    except (subprocess.CalledProcessError, ValueError) as exc:
        raise FingerprintFailure("Audio could not be decoded. Check FFmpeg/ffprobe and file permissions.") from exc
    if samples.size > SAMPLE_RATE * limit:
        raise FingerprintFailure(f"Decoded audio must have a duration of at most {limit:g} seconds.")
    return _checked_samples(samples)


def _checked_samples(samples: ArrayLike) -> NDArray[np.float32]:
    try:
        samples = np.asarray(samples)
        if samples.ndim != 1 or samples.dtype.kind not in "fiu":
            raise FingerprintFailure("Canonical audio must be a one-dimensional array of real mono samples.")
        if samples.size < WINDOW_SIZE:
            raise FingerprintFailure(f"Audio must contain at least {WINDOW_SIZE} samples at {SAMPLE_RATE} Hz.")
        if samples.size > SAMPLE_RATE * MAX_DURATION:
            raise FingerprintFailure("Decoded audio must have a duration of at most 15 minutes.")
        with np.errstate(over="raise", invalid="raise"):
            samples = samples.astype(np.float32, copy=False)
    except (TypeError, ValueError, FloatingPointError) as exc:
        if isinstance(exc, FingerprintFailure):
            raise
        raise FingerprintFailure("Canonical audio must contain finite real samples.") from exc
    if not np.isfinite(samples).all():
        raise FingerprintFailure("Canonical audio must contain finite real samples.")
    return samples


def _magnitude(samples: NDArray[np.float32], first: int, stop: int) -> NDArray[np.float32]:
    section = samples[first * HOP_SIZE:(stop - 1) * HOP_SIZE + WINDOW_SIZE]
    frames = sliding_window_view(section, WINDOW_SIZE)[::HOP_SIZE]
    window = hamming(WINDOW_SIZE, sym=False).astype(np.float32)
    magnitude = np.abs(rfft(frames * window, axis=1))
    if not np.isfinite(magnitude).all():
        raise FingerprintFailure("Audio magnitude could not be computed from these samples.")
    magnitude[:, 0] = 0
    return magnitude


def _peaks(samples: NDArray[np.float32]) -> Iterator[tuple[int, int]]:
    total_frames = 1 + (samples.size - WINDOW_SIZE) // HOP_SIZE
    halo = FILTER_FRAMES // 2
    # Align blocks to global peak intervals so batching never changes selection.
    block_frames = max(PEAK_INTERVAL, BLOCK_FRAMES // PEAK_INTERVAL * PEAK_INTERVAL)
    for first in range(0, total_frames, block_frames):
        stop = min(first + block_frames, total_frames)
        context_first = max(0, first - halo)
        magnitude = _magnitude(samples, context_first, min(stop + halo, total_frames))
        maxima = (magnitude > 0) & (magnitude == maximum_filter(
            magnitude, size=(FILTER_FRAMES, FILTER_BINS), mode="constant", cval=0,
        ))
        selected = []
        for interval in range(first, stop, PEAK_INTERVAL):
            start = interval - context_first
            end = min(interval + PEAK_INTERVAL, stop) - context_first
            for low, high in zip(BAND_EDGES, BAND_EDGES[1:]):
                candidates = np.where(maxima[start:end, low:high], magnitude[start:end, low:high], 0)
                flat_index = int(candidates.argmax())
                frame, frequency = divmod(flat_index, high - low)
                if candidates[frame, frequency] > 0:
                    selected.append((interval + frame, low + frequency))
        yield from sorted(selected)


def _pair_peaks(peaks: Iterable[tuple[int, int]]) -> Iterator[Fingerprint]:
    peaks = iter(peaks)
    pending: deque[tuple[int, int]] = deque()
    next_peak = next(peaks, None)
    while next_peak is not None or pending:
        if next_peak is not None and (not pending or next_peak[0] <= pending[0][0] + TARGET_FRAMES):
            pending.append(next_peak)
            next_peak = next(peaks, None)
            continue
        anchor_frame, anchor_bin = pending.popleft()
        for target_frame, target_bin in pending:
            delta = target_frame - anchor_frame
            if delta > 0:
                yield (anchor_bin, target_bin, delta), anchor_frame


def fingerprint_samples(samples: ArrayLike) -> Iterator[Fingerprint]:
    """Yield keys and anchor frames from mono 8,192-Hz PCM; validation is lazy."""
    yield from _pair_peaks(_peaks(_checked_samples(samples)))


def fingerprint_audio(path: Path) -> Iterator[Fingerprint]:
    """Yield the same fingerprints for a validated local catalog or query file."""
    yield from fingerprint_samples(decode_audio(path))
