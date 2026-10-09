# Magikazaam

A playlist-based song recognition app. Import authorized audio from a YouTube playlist through the React client, Python HTTP API, or CLI. The shared fingerprint pipeline indexes source audio and metadata in SQLite. The client displays a persistent catalog and identifies temporary microphone recordings against it.

## Run locally

Install Python 3.12+, uv, Node.js 22.12+ (Node 24 recommended), npm, FFmpeg, and ffprobe. The importer uses the FFmpeg executables, not a Python package named `ffmpeg`.

From the repository root:

```bash
uv sync
uv run magikazaam
```

In a second terminal:

```bash
cd frontend
npm ci
npm run dev
```

Open [the client](http://127.0.0.1:5173). The API listens on `127.0.0.1:8001`; Vite proxies `/api` requests to it. Both ports must be available. Run one API worker. These are local development servers, not a production deployment.

## Import a playlist

Paste a public or unlisted YouTube playlist URL or ID into the client and choose **Import playlist**. The client shows progress and each track's result. Private, deleted, age-restricted, and otherwise inaccessible tracks report failures; no account cookies are used.

The same importer runs without the client:

```bash
uv run magikazaam import-playlist '<playlist-url-or-id>'
```

The CLI prints a JSON result when finished and exits with status `1` if the playlist or any track failed, otherwise `0`.

Only use tracks you are authorized to acquire through this download method. Access to playlist metadata or a public video does not establish authorization to download its audio. The app does not determine those rights for you.

### Limits and audio formats

- At most **100 playlist entries**. Larger playlists are rejected before any track downloads.
- Each track must be **greater than zero and at most 15 minutes**.
- Each audio file must be **nonempty and at most 50 MiB**. Downloads exceeding the limit are stopped and removed.
- Audio-only **M4A/AAC** or **WebM/Opus**, downloaded without transcoding. Files must pass codec, duration, and full decoding checks.
- Live/upcoming streams and unsupported formats fail individually. Existing video IDs, including repeated playlist entries, are skipped before downloading. Imports add tracks to the existing catalog rather than replacing it.

yt-dlp reads metadata and downloads audio; its bundled EJS dependency uses the installed Node runtime, which is enabled explicitly. FFmpeg/ffprobe validate actual audio instead of relying only on filenames or metadata. NumPy and SciPy implement the shared fingerprint pipeline.

### Catalog storage and compatibility

`data/catalog.sqlite3` stores track metadata, original authorized audio BLOBs, the audio format, fingerprint postings, and the processing profile. It and its SQLite sidecar files are ignored by Git. Source audio stays in the database so a future rebuild can regenerate fingerprints without another download.

Each track and all its postings commit together. Validation, decoding, fingerprint, or database failures roll back that track; earlier successes remain and later entries are still attempted. Audio with no fingerprints fails indexing. Repeated fingerprint keys retain their distinct anchor frames, and a non-unique key index supports lookup. Posting track IDs reference the track's YouTube video ID with cascading deletion.

Catalog keys pack the shared tuple as `(anchor_bin << 16) | (target_bin << 6) | delta_frames`, using 10 bits per frequency bin and 6 bits for the delta. The supported ranges are bins 1–512 and deltas 1–50. Recognition uses the same encoder and checks the stored profile before lookup.

New databases record the current fingerprint profile. Missing, malformed, or incompatible profiles block imports and catalog reads with a rebuild-required error; they are never silently replaced. An explicit rebuild command is deferred. Keep the database and its original audio safe while resolving a mismatch.

### Temporary files and restart behavior

New imports use temporary downloads under `data/imports/<import_id>/`. Audio is removed after processing, including handled failures; the atomically replaced `manifest.json` retains metadata and per-track results. Existing staged files from earlier preparation imports are left untouched and are not migrated automatically.

The catalog survives server and browser restarts. API job progress lives in memory and resets on server restart. Automatic resume/recovery is not implemented. Forced termination may leave a `.partial-*` directory and a manifest marked `running`; inspect interrupted imports manually. Earlier committed tracks remain available and are skipped on re-import.

Per-file limits do not bound total catalog size: multiple imports accumulate audio BLOBs. Keep the database on persistent storage and measure its size and backup time during task 6 evaluation.

## Identify a song

Choose **Identify Song** and allow microphone access. The client records about 10 seconds, stops the microphone, and searches your indexed catalog. It displays the matched title and available channel with a source link, or **No match found**. Permission, recording, validation, and server errors appear separately; retry starts a new recording. **Cancel** works during permission, recording, and processing.

Microphone access requires localhost or HTTPS and a browser supporting MediaRecorder with WebM/Opus or MP4/AAC. The browser chooses a supported recording format; the API accepts audio-only clips up to **5 MiB** with **2–12 seconds of decoded audio**. Missing duration metadata in browser WebM is supported; actual decoded duration is still checked. Catalog imports keep their stricter metadata requirements and existing 15-minute/50 MiB limits.

Query uploads use private system temporary directories, removed after processing or failure. Browser chunks/Blob references are released, timers cleared, microphone tracks stopped, and uploads aborted on cancellation or unmount. A cancelled backend task waits for its decoder worker before deleting the worker's input. Neither audio nor query hashes are stored in SQLite or listening history. Abrupt process/OS termination cannot guarantee temporary-file cleanup.

The matcher uses distinct query occurrences in the strongest offset cluster (center ±1 STFT frame), normalized by every usable query occurrence, including keys without catalog hits. The fixture-calibrated baseline requires 16 aligned votes, support at least `16 / 2303` (about 0.695%), and a support gap of at least `14 / 2303` (about 0.608 percentage points) over the runner-up. Thresholds are **only a synthetic fixture baseline**, not validated real microphone settings; see [calibration and limitations](docs/algorithm.md#task-4-local-calibration).

## Shared fingerprint pipeline

`magikazaam.fingerprint` provides the same processing for catalog audio and microphone queries:

```python
from pathlib import Path
from magikazaam.fingerprint import fingerprint_audio, fingerprint_profile, check_fingerprint_profile

profile = fingerprint_profile()  # Persisted in catalog_meta for new catalogs.
check_fingerprint_profile(profile)  # Check stored metadata before indexing or matching.
for key, anchor_frame in fingerprint_audio(Path("authorized-track.m4a")):
    # key is (anchor_frequency_bin, target_frequency_bin, delta_frames).
    print(key, anchor_frame)
```

`decode_audio(path)` returns validated mono float32 PCM at 8,192 Hz, without writing decoded audio to disk. `fingerprint_samples(samples)` accepts already canonicalized real mono samples at that rate. Both fingerprint entry points yield records lazily; consume the iterator to process audio and receive validation/decoding errors (`FingerprintFailure`). Audio must contain at least one complete 1,024-sample window (125 ms), and the existing file and 15-minute duration limits apply. Silence produces no fingerprints.

The pipeline uses a periodic 1,024-sample Hamming window, a 32-sample hop, and no padding. It excludes DC and selects positive local maxima across six logarithmic bands using a 25-frame × 9-bin filter. Each band retains at most one peak per 25-frame interval, with ties resolved by earliest frame then lowest bin. Each anchor pairs with all selected peaks 1–50 frames ahead. Frequency bins and frame deltas use their exact integer values, and repeated keys retain their separate anchor frames.

Spectrograms use blocks of 2,500 frames with overlapping filter context; peak pairs span block boundaries. PCM stays in memory, while fingerprints stream rather than accumulating a full track's postings. These peak-selection settings are provisional and require evaluation on actual music and microphone clips.

The serialized profile covers fingerprint generation settings. Missing, malformed, or different profiles raise a rebuild-required error; processing block size is not a profile setting because it does not change the output. The catalog persists this profile and checks compatibility before indexing and catalog reads. Recognition checks the same profile before lookup.

## HTTP API

| Request | Response |
| --- | --- |
| `POST /api/identify` with a raw audio body and `Content-Type: audio/webm` or `audio/mp4` (codec parameters allowed) | `200`, `{"status":"match","track":{...}}` or `{"status":"no_match"}` |
| `GET /api/catalog` | `200`, a list of indexed track metadata; an absent catalog returns `[]` |
| `GET /api/health` | `200`, `{"status":"ok"}` |
| `POST /api/imports` with `{"playlist":"<url-or-id>"}` | `202`, `{"import_id":"..."}` |
| `GET /api/imports/<import_id>` | `200`, import state, progress, track metadata and results |

Import states are `queued`, `running`, `completed`, and `failed`. Each track is `indexed`, `skipped` (already indexed), or `failed`, with an error on failure. `completed` means all entries were processed; individual tracks may still have failed. Status and catalog responses expose no audio BLOBs, fingerprints, or filesystem paths. Catalog items contain `video_id`, `title`, `channel`, and `source_url`.

Invalid requests return `422`; a second active import returns `409`; unknown IDs return `404`. Catalog read/profile failures return `409` with an actionable message. Errors discovered after starting appear in the job's terminal status. The client polls once per second and offers a retry if progress cannot be refreshed. Interactive API docs are at [localhost `/docs`](http://127.0.0.1:8001/docs).

Identification returns only track metadata (`video_id`, `title`, `channel`, `source_url`), without scores, audio, hashes, or paths. An absent/empty catalog, silence, weak evidence, or ambiguity returns normal `200` no-match. Identification errors use `400` for an interrupted upload, `413` for size, `415` for unsupported media types, `422` for invalid audio/codec/container/duration, `409` for catalog/profile errors, `503` for unavailable/timed-out audio tools, and `500` for unexpected processing errors. Error bodies use `{"detail":"..."}`.

## Verify

```bash
uv run pytest -q
uv build
cd frontend
npm run build
```

The frontend build includes TypeScript checking. Backend tests mock YouTube access and exercise limits, temporary cleanup, duplicate skips, per-track rollback and continuation, concurrent duplicate writes, profile rejection, indexed SQLite lookup, cascading deletion, API progress/catalog persistence, CLI reuse, and real AAC/Opus decoding and indexing. Fingerprint tests cover STFT framing, peak selection, repeatability, volume scaling, pairing limits, excerpt offsets, processing-block equivalence, and profile compatibility. Real decoder tests require FFmpeg and ffprobe; otherwise they are skipped.

Task 3 verification: **135 backend tests passed**, the Python package and frontend build successfully, and desktop/mobile browser checks exercise imports, indexed/skipped/failed results, catalog reload, and profile-error retry using generated local audio and mocked YouTube metadata. The optional bundled UI detector was unavailable; layout and states were checked in the browser.

Task 4 verification: **160 backend tests pass**, Python package build and frontend type-check/build pass. Tests cover vote deduplication, ±1-frame alignment, normalized support, thresholds, ambiguity, bounded lookup batches, query formats and durations, and cleanup on success, failure, interrupted upload, and task cancellation. Generated AAC catalog audio and browser-style streaming Opus queries exercise the real decoder and recognition API.

Desktop/mobile browser checks used generated local audio: denied/late permission, cancellation, recording progress, automatic stop, processing, error/retry, match/no-match, and unmount cleanup. The actual browser MediaRecorder encoded a generated Web Audio stream and matched through the API. This verifies browser encoding, not physical microphone acoustics or Safari hardware. The optional UI detector was unavailable; layout was inspected in the browser.

Reproduce local threshold calibration with `.venv/bin/python tests/calibrate_recognition.py` after installing development dependencies. Generated audio and databases live only in temporary directories. Task 6 remains open.

Task 6 now has an offline labeled-clip evaluator: `uv run python tests/evaluate_recognition.py --catalog data/catalog.sqlite3 --manifest data/evaluation/manifest.json --output data/evaluation/report-001.json`. It compares baseline and calibration-only tuned thresholds on separate songs, reports accuracy/errors and median/p95 backend processing time, and measures an integrity-checked temporary SQLite backup. It preserves source recordings and the catalog, removes its snapshot, and does not change production thresholds. See [the collection guide, manifest and adoption gates](docs/evaluation.md). The intended authorized playlist and real microphone clips are still pending.

No live YouTube import has been verified without a supplied playlist authorized for this acquisition method. Extractor changes can require updating yt-dlp: run `uv lock --upgrade-package yt-dlp`, then `uv sync` and recheck an authorized playlist.

## Product specification

See [the client brief](docs/client-brief.md), [architecture](docs/architecture.md), [recognition algorithm](docs/algorithm.md), and [remaining tasks](todo.md). Recognition uses a shared landmark fingerprint pipeline and a bounded SQLite catalog, with temporary microphone recordings and no listening history.
