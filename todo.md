# Magikazaam TODO

This checklist tracks the MVP defined in [the client brief](docs/client-brief.md), [the architecture](docs/architecture.md), and [the algorithm spec](docs/algorithm.md). A selected YouTube playlist is the catalog source. The app foundation includes the React client, Python API, shared playlist preparation importer, and shared fingerprint pipeline. The SQLite catalog and indexing flow are implemented; recognition and the minimum identification flow are implemented with a local fixture baseline; real microphone evaluation remains task 6.

## 1. Set up the app

- [x] Create the React client and connect it to a Python HTTP API.
- [x] Create a Python playlist-import script and make it callable by the API for frontend imports.
- [x] Choose the smallest suitable Python API framework, audio decoder, and signal-processing libraries.
- [x] Choose the authorized method for obtaining audio for each YouTube playlist entry; playlist metadata access alone must not be treated as permission to download audio.
- [x] Decide which audio formats and size/duration limits the importer supports.

Task 1 uses FastAPI/Uvicorn, React/TypeScript/Vite, yt-dlp for public/unlisted playlists authorized for this download method, and FFmpeg/ffprobe for validation. NumPy/SciPy are selected for task 2. Preparation supports audio-only M4A/AAC or WebM/Opus, with 100 entries, 15 minutes per track, and 50 MiB per file. Prepared audio and manifests remain in ignored staging; indexing is not implemented yet. Verification: 49 backend tests, Python package build, frontend type-check/build, and desktop/mobile browser checks with generated local audio. Live YouTube importing remains unverified without a supplied authorized playlist. See [setup and commands](README.md).

## 2. Build the shared fingerprint pipeline

- [x] Canonicalize catalog and microphone audio the same way: decode, downmix to mono, and resample to 8,192 Hz.
- [x] Compute the magnitude STFT with a 1,024-sample Hamming window and 32-sample hop.
- [x] Select sparse local peaks across six logarithmic frequency bands using maximum filtering, with coverage across time and frequency.
- [x] Pair each anchor peak with selected peaks in the next 50 frames.
- [x] Generate deterministic keys from `(anchor_frequency_bin, target_frequency_bin, delta_frames)` and retain each anchor frame.
- [x] Store the fingerprint profile/version with the catalog and prevent matching with an incompatible index.

Task 2 implements `magikazaam.fingerprint` with NumPy/SciPy, shared FFmpeg decoding to in-memory mono float32 PCM, bounded spectrogram blocks, deterministic peak selection, and streamed landmark records. Profile serialization and compatibility rejection are implemented and tested; task 3 now persists the profile and enforces checks at catalog operations. Peak/filter defaults remain provisional pending task 6 evaluation. See [the pipeline interface](README.md#shared-fingerprint-pipeline) and [chosen profile](docs/algorithm.md#shared-processing-profile).

Verification: 107 backend tests pass (58 fingerprint tests plus the existing 49), including real generated stereo AAC/Opus decoding, and the Python package builds. A 15-minute synthetic PCM input streamed 343,134 fingerprints in about 2.6 seconds with roughly 185 MiB peak process memory on the development machine; this is a processing smoke check, not a recognition-quality evaluation. Live YouTube and microphone evaluation remain unverified.

## 3. Index the playlist catalog

- [x] Accept a YouTube playlist URL or ID and read its track IDs plus available title, channel/artist, and source video URL metadata.
- [x] Import audio only through the selected authorized method; report entries whose audio cannot be obtained.
- [x] Create SQLite tables for tracks, audio BLOBs, fingerprint postings, and fingerprint profile metadata.
- [x] Add a non-unique index on the fingerprint key and a track-ID reference for each posting.
- [x] Store authorized source audio as a BLOB in SQLite with its metadata and fingerprints so it can be re-indexed if the profile changes.
- [x] Write each audio BLOB, track row, and its postings atomically; report per-track failures without partial database records.
- [x] Show import progress, indexed tracks, and useful per-track errors in the client.

Task 3 implements `magikazaam.catalog` with SQLite source-audio BLOBs, metadata, streamed fingerprint postings, collision-free integer keys, a non-unique lookup index, and persisted profile checks. Each track commits atomically; failures roll back and later entries continue. Imports accumulate tracks, skip existing video IDs before downloading, and remove new temporary audio after processing. Existing staging is untouched. The CLI and API share `import_playlist`; `GET /api/catalog` and the client list expose persistent metadata with indexed/skipped/failed results. Profile mismatches preserve data and block use; rebuild tooling remains deferred.

Verification: 135 backend tests pass, including real generated AAC/Opus indexing, transaction rollback, concurrent duplicate protection, profile rejection, API catalog restart persistence, and CLI statuses. Python package and frontend type-check/build pass. Desktop/mobile browser checks use generated local audio and mocked YouTube metadata for mixed import results, catalog reload, and profile-error retry. Live YouTube imports and microphone recognition quality remain unverified.

## 4. Implement recognition

- [x] Record about 10 seconds only after the user starts identification and grants microphone permission.
- [x] Validate the uploaded clip, run the shared fingerprint pipeline, and discard the clip and query hashes after processing—even on failure.
- [x] Look up query keys in SQLite and calculate `catalog_anchor_frame - query_anchor_frame` for each posting.
- [x] Group votes by track and nearby offset; start with a one-frame offset neighborhood.
- [x] Score the best offset cluster using aligned votes and support ratio; return a match only when calibrated thresholds pass and the result is not ambiguous.
- [x] Return **No match found** for absent or weak evidence, separately from microphone, input-validation, and processing errors.

Task 4 implements the raw-audio recognition API, bounded query decoding (5 MiB and 2–12 seconds), profile-checked SQLite lookup, distinct-occurrence ±1-frame offset scoring, and fixture-calibrated vote/support/ambiguity thresholds. The client records about 10 seconds after user action, supports Cancel, distinguishes match/no-match/errors, and releases microphone resources and query data. Request cleanup covers success, failure, interrupted upload, and cancellation while a worker is processing. No catalog schema or fingerprint-profile change is needed.

Verification: 160 backend tests pass; Python package and frontend type-check/build pass. Calibration identifies 48/48 positives and rejects 10/10 negative/ambiguous cases; held-out fixtures identify 47/48 positives and reject 10/10 negative/ambiguous cases, with zero incorrect matches. Desktop/mobile browser checks cover the flow; actual browser MediaRecorder encoding of generated audio matches through the API. Physical microphone/music quality and Safari hardware remain unverified. See [local calibration](docs/algorithm.md#task-4-local-calibration).

## 5. Complete the user flow

- [x] Build a playlist URL/ID form and an import action.
- [x] Show import status, per-track success/failure, and the resulting catalog list.
- [x] Build the **Identify Song** flow, including microphone permission, recording progress, and processing state.
- [x] Display the matched title and available artist/channel information, or a clear **No match found** result.
- [x] Make the microphone recording temporary in both the browser and backend; do not save it as history.

Task 4 supplies this minimum user flow using the existing page styling. Further visual redesign was not added.

## 6. Evaluate and tune

An offline evaluation workflow is implemented in `tests/evaluate_recognition.py`: validated labeled clips with source-song split protection, baseline and calibration-only threshold comparison, held-out adoption gates, per-condition accuracy/errors, median/p95 backend processing time, and an integrity-checked temporary SQLite backup. It reuses the existing pipeline and calibration logic, preserves supplied recordings and the source catalog, and removes its snapshot. See [the evaluation guide](docs/evaluation.md). The authorized playlist and labeled real microphone clips remain pending, so the checklist below is still open and production thresholds/profile are unchanged.

Workflow verification: **195 backend tests pass** (35 new evaluation tests), and the Python package builds. The original synthetic calibration still reproduces 48/48 calibration positives and 47/48 held-out positives, rejecting 10/10 no-match cases in each set. A separate 28-clip generated evaluator smoke run identified 12/12 calibration and 11/12 held-out positives with no incorrect matches; its tuned candidate failed adoption, leaving the existing thresholds recommended. Catalog preservation, report overwrite protection and snapshot cleanup are verified. These checks do not establish real music/microphone accuracy or intended-catalog size and backup performance.

- [ ] Prepare labeled positive clips from different points in indexed tracks and negative clips from tracks outside the catalog.
- [ ] Include representative microphone noise, room acoustics, volume changes, and common compression in the evaluation set.
- [ ] Measure correct identification rate, false-positive rate, and processing time.
- [ ] Measure SQLite database size and backup time with the intended playlist catalog.
- [ ] Tune peak filtering/density, frequency and time quantization, offset tolerance, and vote/support/ambiguity thresholds against evaluation results.
- [ ] Record the chosen profile and thresholds; re-index catalog tracks whenever fingerprint-generation settings change.
- [ ] Confirm the MVP recognizes indexed clips from different positions, rejects out-of-catalog clips, and does not retain microphone recordings.

## Out of MVP scope

Global music search, lyrics or artist discovery, cover/remix matching, accounts, listening history, ML/LLM recognition, and distributed deployment.
