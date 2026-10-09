# Task 6: Offline recognition evaluation

This workflow measures the existing fingerprint profile and ±1-frame offset matcher against a labeled catalog. It compares the current synthetic-fixture thresholds with a candidate calibrated on separate songs. It does not change the application, persist live microphone queries, or automatically install thresholds. Real music, physical microphones, intended-catalog size and production accuracy remain unverified until an authorized playlist and labeled clips are supplied.

## Collect a separate evaluation set

1. Import the intended playlist using the existing importer, only when its tracks are authorized for the selected downloader. Use the full intended catalog for both calibration and held-out recognition, including tracks that are not represented by query clips. Record which entries failed import; positive labels must refer to successfully indexed video IDs.
2. Assign source songs to calibration or held-out before collecting variants, approximately 70/30. Keep every excerpt, recording, compression variant and alternate ID of the same underlying recording in one split. Both splits need positive and negative/no-match clips. Do not change the split after seeing results.
3. Collect approximately 10-second positive excerpts from beginning, middle and late positions. Avoid excerpts beyond the track's end. Include normal and quiet playback, representative background noise, different speaker-to-microphone distances and rooms, and the intended browser/device combinations. Record exact offsets and equipment in your collection notes.
4. Collect actual speaker-to-microphone recordings with a separate audio recorder that exports audio-only M4A/AAC or WebM/Opus. Label these conditions clearly, for example `microphone/phone/quiet-room`, distinct from `generated/quiet` or `direct/opus`. The app's Identify Song flow discards its recording and provides no dataset export. Synthetic noise and compression variants test particular mechanics; they do not substitute for room acoustics or physical microphones.
5. Collect similarly varied negative clips from authorized music outside the catalog. Confirm they are not another title/ID for an indexed recording. Include silence and ambiguous duplicate recordings as additional expected no-match cases, labeled separately from outside-catalog music. Group an ambiguous clip with any other excerpts from its underlying recording.
6. Keep originals and labels under ignored `data/evaluation/`, or outside the repository. This is an explicitly collected offline dataset, separate from ordinary app queries. The evaluator preserves supplied files; you manage their retention. It creates no new audio files and never saves query fingerprints.

Use the production query limits: nonempty files up to 5 MiB and 2–12 decoded seconds, with about 10 seconds preferred. AAC files must have `.m4a` names; WebM/Opus must have `.webm` names. Content, codec, container and decoded duration are checked by the existing backend decoder. Missing browser WebM duration metadata is supported. Unsupported content, excessive duration and processing failures appear as errors, not no-match outcomes.

## Manifest and command

Create a JSON array. Paths are relative to the manifest's directory unless absolute. Replace the example IDs with actual indexed video IDs and add the recording conditions/positions collected above:

```json
[
  {
    "case": "song-a-beginning-mic",
    "path": "clips/song-a-beginning.m4a",
    "expected": "videoAAAAAA",
    "condition": "microphone/laptop/quiet-room",
    "source_group": "song-a-recording",
    "split": "calibration"
  },
  {
    "case": "outside-a-mic",
    "path": "clips/outside-a.webm",
    "expected": null,
    "condition": "microphone/laptop/background-noise",
    "source_group": "outside-a-recording",
    "split": "calibration"
  },
  {
    "case": "song-b-middle-mic",
    "path": "clips/song-b-middle.webm",
    "expected": "videoBBBBBB",
    "condition": "microphone/laptop/quiet-room",
    "source_group": "song-b-recording",
    "split": "held_out"
  },
  {
    "case": "outside-b-mic",
    "path": "clips/outside-b.m4a",
    "expected": null,
    "condition": "microphone/laptop/background-noise",
    "source_group": "outside-b-recording",
    "split": "held_out"
  }
]
```

All six fields are required. `expected: null` means the clip should produce no match. Case names and file paths must be unique; direct file symlinks are rejected. The evaluator rejects source groups, positive track IDs or byte-identical audio shared between splits. Group labels remain your responsibility: the tool cannot detect the same outside-catalog song re-encoded under a different name. Each split must contain at least one positive and one no-match example; this structural minimum does not establish statistical confidence.

Run from the repository root after installing development dependencies:

```bash
uv run python tests/evaluate_recognition.py \
  --catalog data/catalog.sqlite3 \
  --manifest data/evaluation/manifest.json \
  --output data/evaluation/report-001.json
```

The catalog must exist and use the current fingerprint profile. The output directory must exist and the output file must be new. Invalid manifests, unavailable catalogs and backup failures exit with an error without producing a report. Per-clip decode failures remain in the report and prevent threshold adoption. A valid report exits successfully even if its adoption gate fails; inspect `adoption.eligible` and `adoption.reasons`.

## Read the report and choose thresholds

- `catalog` records track count, serialized fingerprint profile, database file bytes, consistent snapshot bytes, backup duration and integrity result. The tool opens the source read-only, uses SQLite's backup API into a private temporary directory, and checks integrity plus foreign-key references. All clip scoring uses that snapshot. Backup duration includes the copy, excludes integrity checking, and measures one run to the system temporary filesystem; record hardware/storage and repeat with new report filenames to assess variance. WAL bytes are not part of `database_bytes`; `snapshot_bytes` includes committed data in the logical backup. The snapshot is removed on success or handled failure.
- `dataset` identifies the exact manifest by SHA-256; each clip includes an audio SHA-256. Keep the manifest, original clips and collection notes to reproduce results. Reports contain labels, aggregate metrics and the top two candidate scores/offsets, but no raw audio, source-audio BLOBs, query hashes or clip paths.
- `baseline` and `candidate` give overall and per-condition metrics for each split. Correct identification rate is correctly identified positives divided by all labeled positives. Negative false-positive rate is returned matches divided by all expected no-match cases, including silence/ambiguity. Wrong-song positive matches, no-match positive misses and processing errors are reported separately. Errors remain in class denominators and never count as successful negative rejection. Check errors alongside rates: zero false positives with failed decoding is not a valid success.
- `processing_seconds` reports successful-clip count, median and nearest-rank p95 for file-size validation, audio validation/decoding, fingerprinting, lookup and the baseline match decision. It excludes upload time, snapshot creation, manifest validation, startup and metadata serialization. Candidate decisions reuse the same scores/timings; this is a backend pipeline measurement, not end-to-end browser latency. Per-clip elapsed times are present for failures too.

Threshold search uses calibration data only. It evaluates observed top-candidate vote/support boundaries plus the existing search floors of 1 vote and 0.01 support. For each pair, find the maximum ambiguity gap of any eligible incorrect candidate; require a strictly larger positive gap. Select the gap retaining the most eligible correct positives, breaking ties toward larger support, votes and gap, in that order. Single-candidate gaps use zero runner-up support. Ties never match. No useful safe separation returns a null candidate instead of installing an all-reject rule. Errors are excluded from fitting and prevent adoption.

Freeze the candidate before processing held-out clips. `adoption.eligible` requires more correctly identified calibration positives than the baseline, zero observed incorrect matches across both splits, no processing errors and at least the baseline's held-out correct-positive count. The report recommends baseline thresholds if any gate fails. It does not assert a numeric production accuracy or latency target, which should be set after the initial real baseline.

When a representative real dataset passes these gates, review its condition results and update only the three constants in `magikazaam.recognition`, then run the backend tests and repeat evaluation. Keep the frozen held-out result as evidence; if you repeatedly tune after looking at held-out failures, collect a fresh held-out set. Small-sample zero incorrect matches does not establish a zero real-world error rate. Generated fixtures alone cannot authorize production tuning.

Keep peak filtering/density, frequency/time quantization, fingerprint generation and offset tolerance unchanged in this pass. Record missed cases and top-candidate evidence that justify future profile experiments. Fingerprint changes require re-indexing all catalog audio with a new compatible profile; match-threshold changes alone do not. Rebuild tooling remains deferred.

## Verification and current limitations

The evaluator is tested with generated, time-varying six-band songs encoded as real AAC catalog audio and AAC/streaming Opus query files. Its integration set contains 24 positives at three offsets under clean, quiet, noisy and Opus conditions, two outside-catalog clips, silence and an ambiguous duplicate recording. Separate tests cover split leakage, manifest validation, metric accounting, threshold selection, adoption failure, report overwrite protection and temporary cleanup. This verifies the workflow, not recognition quality on real music.

Verification on 2026-10-09: **195 backend tests passed**, including 35 evaluation tests; the Python source distribution and wheel built successfully. Threaded API tests required execution outside the sandbox. The existing FastAPI/Starlette TestClient emitted one dependency deprecation warning; no dependency change was needed for this work. The original synthetic calibration thresholds and 48/48 calibration, 47/48 held-out results reproduced exactly.

The existing synthetic calibration remains reproducible with `uv run python tests/calibrate_recognition.py`. Until the authorized playlist and labeled real recordings arrive, task 6's real-microphone collection, intended-catalog measurement, real threshold adoption and final MVP acceptance remain open.

A generated smoke run on 2026-10-09 identified 12/12 calibration positives and 11/12 held-out positives, with zero incorrect matches and zero processing errors across 28 clips. The four-track database and verified snapshot were each 602,112 bytes. Calibration median/p95 pipeline time was approximately 0.136/0.186 seconds; held-out time was 0.139/0.149 seconds. A single backup to the development machine's temporary filesystem took approximately 0.00058 seconds. This tiny synthetic catalog is a workflow smoke check, not an intended-catalog storage or latency benchmark. The candidate failed the adoption gate because calibration recognition did not improve and held-out recognition regressed; the existing thresholds remain recommended. The local report is `data/evaluation/generated-smoke-report.json`; all source fixtures and the backup were temporary and have been removed.
