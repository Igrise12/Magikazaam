import json
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

import numpy as np
import pytest

from magikazaam import fingerprint as fp


@pytest.fixture
def samples():
    return np.random.default_rng(123).normal(0, 0.1, fp.SAMPLE_RATE * 12).astype(np.float32)


def test_stft_uses_periodic_hamming_complete_windows_and_exact_hop():
    frequency_bin = 40
    count = fp.WINDOW_SIZE + 3 * fp.HOP_SIZE - 1
    audio = np.sin(2 * np.pi * frequency_bin * np.arange(count) / fp.WINDOW_SIZE).astype(np.float32)
    total_frames = 1 + (audio.size - fp.WINDOW_SIZE) // fp.HOP_SIZE
    magnitude = fp._magnitude(audio, 0, total_frames)
    assert magnitude.shape == (3, 513)
    assert np.all(magnitude.argmax(axis=1) == frequency_bin)
    np.testing.assert_allclose(magnitude[:, frequency_bin], fp.WINDOW_SIZE * 0.54 / 2, rtol=1e-6)
    assert np.all(magnitude[:, 0] == 0)


def test_peak_selection_covers_six_bands_and_time_intervals(monkeypatch):
    spectrum = np.zeros((100, 513), dtype=np.float32)
    bins = (1, 6, 16, 40, 100, 300)
    for frame in (12, 37, 62, 87):
        spectrum[frame, list(bins)] = np.arange(1, 7)
    monkeypatch.setattr(fp, "_magnitude", lambda samples, first, stop: spectrum[first:stop].copy())
    audio = np.zeros(fp.WINDOW_SIZE + 99 * fp.HOP_SIZE, dtype=np.float32)
    assert list(fp._peaks(audio)) == [(frame, frequency) for frame in (12, 37, 62, 87) for frequency in bins]


def test_peak_ties_choose_earliest_frame_then_lowest_bin(monkeypatch):
    spectrum = np.zeros((50, 513), dtype=np.float32)
    spectrum[10:12, [32, 38]] = 5
    spectrum[35, 32] = 2
    spectrum[40, 38] = 3
    monkeypatch.setattr(fp, "_magnitude", lambda samples, first, stop: spectrum[first:stop].copy())
    audio = np.zeros(fp.WINDOW_SIZE + 49 * fp.HOP_SIZE, dtype=np.float32)
    assert list(fp._peaks(audio)) == [(10, 32), (40, 38)]


def test_maximum_filter_suppresses_weaker_neighbor(monkeypatch):
    spectrum = np.zeros((50, 513), dtype=np.float32)
    spectrum[24, 40] = 5
    spectrum[26, 42] = 4
    monkeypatch.setattr(fp, "_magnitude", lambda samples, first, stop: spectrum[first:stop].copy())
    audio = np.zeros(fp.WINDOW_SIZE + 49 * fp.HOP_SIZE, dtype=np.float32)
    assert list(fp._peaks(audio)) == [(24, 40)]


def test_pairing_includes_frames_one_to_fifty_and_excludes_same_frame():
    peaks = [(0, 10), (0, 11), (1, 12), (50, 13), (51, 14), (101, 15), (102, 16)]
    assert list(fp._pair_peaks(peaks)) == [
        ((10, 12, 1), 0), ((10, 13, 50), 0),
        ((11, 12, 1), 0), ((11, 13, 50), 0),
        ((12, 13, 49), 1), ((12, 14, 50), 1),
        ((13, 14, 1), 50), ((14, 15, 50), 51), ((15, 16, 1), 101),
    ]


def test_repeated_keys_keep_each_anchor_frame():
    assert list(fp._pair_peaks([(0, 10), (1, 11), (100, 10), (101, 11)])) == [
        ((10, 11, 1), 0), ((10, 11, 1), 100),
    ]
    assert list(fp._pair_peaks([])) == []
    assert list(fp._pair_peaks([(0, 10)])) == []


def test_repeatable_fingerprints_and_volume_scaling(samples):
    records = list(fp.fingerprint_samples(samples))
    assert records
    assert records == list(fp.fingerprint_samples(samples))
    assert records == list(fp.fingerprint_samples(samples * 0.5))
    assert all(1 <= key[0] <= 512 and 1 <= key[1] <= 512 and 1 <= key[2] <= 50
               and isinstance(frame, int) for key, frame in records)


@pytest.mark.parametrize("block_frames", [50, 73, 2500])
def test_blocks_preserve_peaks_and_pairs_across_boundaries(samples, monkeypatch, block_frames):
    total_frames = 1 + (samples.size - fp.WINDOW_SIZE) // fp.HOP_SIZE
    monkeypatch.setattr(fp, "BLOCK_FRAMES", 5000)
    expected = list(fp.fingerprint_samples(samples))
    monkeypatch.setattr(fp, "BLOCK_FRAMES", block_frames)
    calls = []
    magnitude = fp._magnitude

    def record_block(audio, first, stop):
        calls.append((first, stop))
        return magnitude(audio, first, stop)

    monkeypatch.setattr(fp, "_magnitude", record_block)
    actual = list(fp.fingerprint_samples(samples))
    assert actual == expected
    assert len(calls) > 1
    assert all(stop - first <= block_frames + 2 * (fp.FILTER_FRAMES // 2) for first, stop in calls)
    assert calls[0][0] == 0 and calls[-1][1] == total_frames
    aligned_block = block_frames // fp.PEAK_INTERVAL * fp.PEAK_INTERVAL
    assert any(anchor // aligned_block != (anchor + key[2]) // aligned_block for key, anchor in actual)


def test_frame_aligned_excerpt_retains_internal_keys_and_offset(samples):
    offset, frames = 250, 500
    excerpt = samples[offset * fp.HOP_SIZE:(offset + frames - 1) * fp.HOP_SIZE + fp.WINDOW_SIZE]
    query = {(key, anchor + offset) for key, anchor in fp.fingerprint_samples(excerpt) if 100 <= anchor < 400}
    catalog = set(fp.fingerprint_samples(samples))
    assert query
    assert query <= catalog


def test_silence_has_no_fingerprints():
    assert list(fp.fingerprint_samples(np.zeros(fp.SAMPLE_RATE, dtype=np.float32))) == []


@pytest.mark.parametrize("audio,message", [
    ([], "at least"), ([0] * 1023, "at least"),
    (np.zeros((1024, 2)), "one-dimensional"),
    (np.full(1024, np.nan), "finite"), (np.full(1024, np.inf), "finite"),
    (np.full(1024, -np.inf), "finite"), (np.full(1024, 1e100), "finite"),
    (np.ones(1024, dtype=complex), "real mono"), (["audio"] * 1024, "real mono"),
])
def test_invalid_canonical_audio_is_an_error(audio, message):
    with pytest.raises(fp.FingerprintFailure, match=message):
        list(fp.fingerprint_samples(audio))


def test_canonical_duration_is_bounded(monkeypatch):
    monkeypatch.setattr(fp, "MAX_DURATION", 1)
    with pytest.raises(fp.FingerprintFailure, match="duration"):
        list(fp.fingerprint_samples(np.zeros(fp.SAMPLE_RATE + 1)))


@pytest.fixture
def encoded_audio(tmp_path):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("FFmpeg and ffprobe are required for real decoder checks")

    def create(suffix=".m4a", codec="aac"):
        path = tmp_path / f"stereo{suffix}"
        subprocess.run([
            "ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i",
            "aevalsrc=0.25*sin(2*PI*440*t)|0.25*sin(2*PI*880*t):s=44100:d=1",
            "-c:a", codec, str(path),
        ], check=True)
        return path

    return create


@pytest.mark.parametrize("suffix,codec", [(".m4a", "aac"), (".webm", "libopus")])
def test_real_decoder_downmixes_resamples_and_uses_shared_pipeline(encoded_audio, suffix, codec):
    path = encoded_audio(suffix, codec)
    audio = fp.decode_audio(path)
    assert audio.ndim == 1 and audio.dtype == np.float32
    assert fp.SAMPLE_RATE * 0.95 <= audio.size <= fp.SAMPLE_RATE * 1.05
    spectrum = np.abs(np.fft.rfft(audio))
    frequencies = np.fft.rfftfreq(audio.size, 1 / fp.SAMPLE_RATE)
    for tone in (440, 880):
        assert spectrum[np.abs(frequencies - tone) < 2].max() > spectrum.max() * 0.4
    expected = list(fp.fingerprint_samples(audio))
    assert expected
    assert list(fp.fingerprint_audio(path)) == expected


@pytest.mark.parametrize("kind", ["empty", "corrupt", "oversized", "symlink", "unsupported"])
def test_file_entry_point_keeps_importer_validation(tmp_path, kind):
    path = tmp_path / "invalid.m4a"
    if kind == "symlink":
        target = tmp_path / "target.m4a"
        target.write_bytes(b"audio")
        path.symlink_to(target)
    else:
        path.write_bytes(b"" if kind == "empty" else b"not audio")
        if kind == "oversized":
            with path.open("wb") as output:
                output.truncate(50 * 1024 * 1024 + 1)
        if kind == "unsupported":
            path = path.rename(path.with_suffix(".mp3"))
    with pytest.raises(fp.FingerprintFailure):
        list(fp.fingerprint_audio(path))


def test_file_rejects_wrong_codec(encoded_audio):
    with pytest.raises(fp.FingerprintFailure, match="AAC"):
        fp.decode_audio(encoded_audio(codec="alac"))


@pytest.mark.parametrize("failure", [
    FileNotFoundError(), subprocess.CalledProcessError(1, "ffmpeg"), subprocess.TimeoutExpired("ffmpeg", 120),
])
def test_decoder_failures_are_useful_errors(monkeypatch, failure):
    monkeypatch.setattr(fp, "validate_audio", lambda path: None)

    def fail(*args, **kwargs):
        raise failure

    monkeypatch.setattr(fp.subprocess, "run", fail)
    with pytest.raises(fp.FingerprintFailure, match="could not be decoded"):
        fp.decode_audio(Path("input.m4a"))


@pytest.mark.parametrize("output,message", [
    (b"bad", "could not be decoded"), (b"", "at least"),
    (np.full(1024, np.nan, dtype="<f4").tobytes(), "finite"),
    (np.zeros(8193, dtype="<f4").tobytes(), "duration"),
])
def test_decoder_checks_actual_pcm(monkeypatch, output, message):
    monkeypatch.setattr(fp, "MAX_DURATION", 1)
    monkeypatch.setattr(fp, "validate_audio", lambda path: None)
    monkeypatch.setattr(fp.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(stdout=output))
    with pytest.raises(fp.FingerprintFailure, match=message):
        fp.decode_audio(Path("input.m4a"))


def test_profile_round_trip_and_processing_blocks_are_not_profile_settings(monkeypatch):
    profile = fp.fingerprint_profile()
    assert fp.fingerprint_profile() == profile
    fp.check_fingerprint_profile(json.dumps(json.loads(profile), indent=2))
    monkeypatch.setattr(fp, "BLOCK_FRAMES", 50)
    assert fp.fingerprint_profile() == profile


@pytest.mark.parametrize("profile", [None, "", "not json", "null", "{}", "[]"])
def test_missing_and_invalid_profiles_require_rebuild(profile):
    with pytest.raises(fp.FingerprintFailure, match="Rebuild the catalog"):
        fp.check_fingerprint_profile(profile)


@pytest.mark.parametrize("section,key,value", [
    (None, "version", 2), ("audio", "sample_rate", 44100), ("audio", "channels", 2),
    ("audio", "resampler", "soxr"), ("stft", "window", "hann"), ("stft", "hop_samples", 64),
    ("stft", "padding", True), ("peaks", "band_edges", [1, 513]),
    ("peaks", "maximum_filter", [9, 9]), ("peaks", "interval_frames", 50),
    ("peaks", "ties", "highest-bin"), ("hash", "target_frames", [1, 100]),
    ("hash", "encoding", "packed"), ("hash", "bin_step", 2),
])
def test_changed_profile_requires_rebuild(section, key, value):
    profile = json.loads(fp.fingerprint_profile())
    (profile if section is None else profile[section])[key] = value
    with pytest.raises(fp.FingerprintFailure, match="Rebuild the catalog"):
        fp.check_fingerprint_profile(json.dumps(profile))
