# Magikazaam TODO

This checklist tracks the MVP defined in [the client brief](docs/client-brief.md), [the architecture](docs/architecture.md), and [the algorithm spec](docs/algorithm.md). A selected YouTube playlist is the catalog source. The repository currently has a Python starter entry point; the React app, API, playlist importer, fingerprint pipeline, and SQLite catalog remain to be built.

## 1. Set up the app

- [ ] Create the React client and connect it to a Python HTTP API.
- [ ] Create a Python playlist-import script and make it callable by the API for frontend imports.
- [ ] Choose the smallest suitable Python API framework, audio decoder, and signal-processing libraries.
- [ ] Choose the authorized method for obtaining audio for each YouTube playlist entry; playlist metadata access alone must not be treated as permission to download audio.
- [ ] Decide which audio formats and size/duration limits the importer supports.

## 2. Build the shared fingerprint pipeline

- [ ] Canonicalize catalog and microphone audio the same way: decode, downmix to mono, and resample to 8,192 Hz.
- [ ] Compute the magnitude STFT with a 1,024-sample Hamming window and 32-sample hop.
- [ ] Select sparse local peaks across six logarithmic frequency bands using maximum filtering, with coverage across time and frequency.
- [ ] Pair each anchor peak with selected peaks in the next 50 frames.
- [ ] Generate deterministic keys from `(anchor_frequency_bin, target_frequency_bin, delta_frames)` and retain each anchor frame.
- [ ] Store the fingerprint profile/version with the catalog and prevent matching with an incompatible index.

## 3. Index the playlist catalog

- [ ] Accept a YouTube playlist URL or ID and read its track IDs plus available title, channel/artist, and source video URL metadata.
- [ ] Import audio only through the selected authorized method; report entries whose audio cannot be obtained.
- [ ] Create SQLite tables for tracks, audio BLOBs, fingerprint postings, and fingerprint profile metadata.
- [ ] Add a non-unique index on the fingerprint key and a track-ID reference for each posting.
- [ ] Store authorized source audio as a BLOB in SQLite with its metadata and fingerprints so it can be re-indexed if the profile changes.
- [ ] Write each audio BLOB, track row, and its postings atomically; report per-track failures without partial database records.
- [ ] Show import progress, indexed tracks, and useful per-track errors in the client.

## 4. Implement recognition

- [ ] Record about 10 seconds only after the user starts identification and grants microphone permission.
- [ ] Validate the uploaded clip, run the shared fingerprint pipeline, and discard the clip and query hashes after processing—even on failure.
- [ ] Look up query keys in SQLite and calculate `catalog_anchor_frame - query_anchor_frame` for each posting.
- [ ] Group votes by track and nearby offset; start with a one-frame offset neighborhood.
- [ ] Score the best offset cluster using aligned votes and support ratio; return a match only when calibrated thresholds pass and the result is not ambiguous.
- [ ] Return **No match found** for absent or weak evidence, separately from microphone, input-validation, and processing errors.

## 5. Complete the user flow

- [ ] Build a playlist URL/ID form and an import action.
- [ ] Show import status, per-track success/failure, and the resulting catalog list.
- [ ] Build the **Identify Song** flow, including microphone permission, recording progress, and processing state.
- [ ] Display the matched title and available artist/channel information, or a clear **No match found** result.
- [ ] Make the microphone recording temporary in both the browser and backend; do not save it as history.

## 6. Evaluate and tune

- [ ] Prepare labeled positive clips from different points in indexed tracks and negative clips from tracks outside the catalog.
- [ ] Include representative microphone noise, room acoustics, volume changes, and common compression in the evaluation set.
- [ ] Measure correct identification rate, false-positive rate, and processing time.
- [ ] Measure SQLite database size and backup time with the intended playlist catalog.
- [ ] Tune peak filtering/density, frequency and time quantization, offset tolerance, and vote/support/ambiguity thresholds against evaluation results.
- [ ] Record the chosen profile and thresholds; re-index catalog tracks whenever fingerprint-generation settings change.
- [ ] Confirm the MVP recognizes indexed clips from different positions, rejects out-of-catalog clips, and does not retain microphone recordings.

## Out of MVP scope

Global music search, lyrics or artist discovery, cover/remix matching, accounts, listening history, ML/LLM recognition, and distributed deployment.
