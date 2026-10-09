# Magikazaam Recognition Algorithm

## Decision

Magikazaam will use **Shazam-style landmark fingerprinting**: sparse peaks from a short-time Fourier transform (STFT) are paired into local hashes, looked up in an inverted index, then verified by a cluster of matching time offsets. The method follows the public constellation and combinatorial-hash approach in Wang's 2003 paper. It is not a copy of Shazam's private production implementation.

This algorithm is the recognition core for the MVP described in [the client brief](client-brief.md), using the processing and storage boundaries in [the architecture](architecture.md).

## Scope

The algorithm will:

- Match a roughly 10-second microphone clip to one track already indexed in the selected playlist catalog.
- Find a matching section wherever it occurs in that track.
- Tolerate moderate changes from microphone capture, background noise, volume, and audio compression; performance must be measured on the project's evaluation set.
- Return track metadata only when matching hashes form sufficient time-aligned evidence; otherwise return **No match found**.

The algorithm will not search a global music service, identify songs from lyrics or melody, or treat covers, remixes, pitch-shifted versions, or substantial edits as the same recording. Those versions must be indexed as separate tracks. No ML or LLM model is part of recognition.

## Fingerprint definition

Each fingerprint is a set of records:

```text
(hash_key, track_id, anchor_frame)
```

The logical `hash_key` is the deterministic tuple:

```text
(anchor_frequency_bin, target_frequency_bin, target_frame - anchor_frame)
```

The three values are quantized using the same settings for catalog and query audio. This is a feature key, not a cryptographic digest. If it is packed into an integer for storage, the encoding must be deterministic and collision-free for the configured ranges. The anchor time is stored separately and is not part of the hash key.

## Shared processing profile

Catalog files and microphone clips must pass through the same pipeline and algorithm configuration. The following values are the project's initial baseline, taken from the Rochester implementation paper; they are starting settings for this implementation, not claimed Shazam constants.

| Stage | Initial setting |
|---|---|
| Audio | Decode, downmix to mono, resample to 8,192 Hz |
| Spectrogram | STFT with a 1,024-sample Hamming window and 32-sample hop |
| Peak map | Divide frequency into six logarithmic bands; keep local maxima using a maximum filter and spread selected peaks across time and frequency |
| Target zone | Pair an anchor with every selected peak in the next 50 frames |
| Hash | Quantized anchor bin, target bin, and frame delta |
| Query duration | About 10 seconds, as specified in the client brief |

Task 2 implements this profile in `magikazaam.fingerprint`. STFT frames begin at sample `frame * 32`, use a periodic Hamming window, and include only complete windows without padding. Frequency bins are 8 Hz apart; bin 0 (DC) is excluded. The rounded logarithmic band edges are `[1, 3, 8, 23, 64, 181, 513]`, using half-open intervals to include bins 1–512.

The initial peak filter spans 25 frames × 9 bins over the whole magnitude map, with zero padding outside its edges. Within each global 25-frame interval and band, retain the strongest positive local maximum, breaking ties by earliest frame and then lowest bin. Emit peaks in frame/bin order. These filter and density choices are provisional implementation defaults, not calibrated recognition settings. Silence has no positive peaks and emits no keys; empty or less-than-one-window input is a validation error.

Pair every anchor with all selected peaks 1–50 frames ahead, excluding peaks in the same frame. Return `((anchor_bin, target_bin, delta_frames), anchor_frame)` with no additional frequency or time quantization. Repeat occurrences of a key at different anchor frames are preserved. Spectrogram blocks overlap for peak filtering, align with global selection intervals, and maintain pairing context across boundaries; block size does not affect fingerprints.

At this sample rate, a frame hop is 32 / 8,192 seconds (about 3.9 ms), and the initial 50-frame target zone spans about 195 ms. The maximum-filter neighborhood, peak density, quantization, and final thresholds still need measurement and tuning; the cited paper does not establish universal values for this project.

Record the selected profile/version in the catalog metadata. Recognition must reject an index made with a different profile or require it to be rebuilt before searching.

`fingerprint_profile()` serializes the current version and all generation settings; `check_fingerprint_profile(stored_profile)` rejects missing, malformed, or different metadata with a rebuild-required error. Task 3 persists the profile in `catalog_meta` and enforces compatibility before catalog reads and writes. Profile mismatches preserve existing data and return a rebuild-required error; recognition checks the same profile; rebuilding remains deferred.

## Catalog indexing

For each authorized catalog audio file:

1. Decode the file, downmix to mono, and resample it using the shared profile.
2. Compute the STFT magnitude spectrogram.
3. Select local time-frequency maxima. Use the frequency bands and neighborhood filtering to retain prominent peaks with coverage throughout the track rather than a dense cluster in one region.
4. For each peak in time order, treat it as an anchor and pair it with every selected peak in the forward target zone. The bounded zone and sparse peak selection constrain the number of pairs.
5. Quantize the two frequency-bin values and their frame difference to produce each `hash_key`.
6. Store every key with the track ID and anchor frame. Keep available title, artist/channel, source URL, and the original source audio BLOB plus its format on the track record.

Task 3 packs keys into integers as `(anchor_bin << 16) | (target_bin << 6) | delta_frames`. Two 10-bit bins and a 6-bit delta preserve every tuple in the current ranges (bins 1–512, deltas 1–50) without collisions. Recognition must use the same encoder.

Store postings in SQLite with a non-unique index on `hash_key`. Repeated keys are allowed: the same pattern can occur at different times or in different tracks. Write a track and all its postings atomically; a failed index operation must not expose a partial fingerprint.

## Microphone query and matching

1. After the user taps **Identify Song** and grants microphone access, record about 10 seconds and send the clip to the Python API.
2. Validate the clip and process it with the same decode, mono, resample, STFT, peak, and hash settings used for the catalog.
3. For each query record `(hash_key, query_anchor_frame)`, look up all catalog postings with that key.
4. For every returned posting, calculate:

   ```text
   offset = catalog_anchor_frame - query_anchor_frame
   ```

5. Group the resulting votes by track and nearby offset. A real excerpt match should create a concentrated offset cluster; incidental matching keys should be spread across offsets.
6. For each track, use the largest offset cluster as its aligned-match score. Count distinct query-hash occurrences in the cluster, and also record its support ratio:

   ```text
   support_ratio = aligned_votes / usable_query_hashes
   ```

   Offset bins use STFT-frame units. Task 4 uses an integer center ±1 frame (three adjacent offsets). A query occurrence counts once across that union. Validate this tolerance on real microphone clips during task 6.
7. Rank candidates by support ratio, using aligned-vote count as a minimum-evidence check and tie-breaker. Return the top track only if it clears calibrated vote and support thresholds and is not ambiguous with another candidate. Otherwise return **No match found**.

Each query-hash occurrence is counted at most once per candidate offset cluster so duplicate database postings do not inflate a score. The query audio and its temporary fingerprints are discarded after processing, including on errors.

## No-match threshold and evaluation

Do not choose a fixed vote count by intuition. Build a labeled evaluation set with:

- Positive clips from different positions in indexed tracks.
- Negative clips from tracks outside the catalog.
- Representative microphone recordings with noise, room acoustics, volume changes, and common compression.

Measure correct identification rate, false-positive rate, and processing time. Set the minimum aligned-vote and normalized-support thresholds (and any ambiguity margin) against positive and negative results, accounting for the size of the actual catalog. Wang's paper uses the raw count in the largest offset cluster as its score and recommends choosing a threshold from the distribution of the highest-scoring incorrect tracks. This MVP also normalizes by usable query hashes, as specified in the client brief. Task 4 selects an initial baseline from generated local fixtures as documented below. These values must not be presented as production-validated; representative real microphone evaluation remains task 6.

## Task 4 local calibration

`magikazaam.recognition` looks up distinct encoded query keys in batches of 500 and retrieves every catalog posting. It calculates catalog minus query anchor frame and finds each track's largest center ±1-frame cluster. Distinct `(key, query_anchor_frame)` occurrences count once per cluster, even when database postings repeat or span neighboring offsets. The denominator includes every usable query occurrence, not only occurrences with lookup hits. Candidates rank by support ratio, then vote count. Tied candidates are rejected by the positive support-gap requirement.

The initial constants are **16 aligned votes**, **support ≥16/2303** (0.0069474598), and **best minus runner-up support ≥14/2303** (0.0060790274). When there is no runner-up its support is zero. Bounds are inclusive. Match thresholds do not change fingerprint generation or require re-indexing; changing the fingerprint profile still does.

Run `.venv/bin/python tests/calibrate_recognition.py` to reproduce calibration. Fixtures contain four independent 24-second generated songs per set, with time-varying six-band tones and envelopes, indexed as real AAC audio. Positive 10-second queries start at 0.1137, 5.333, and 12.731 seconds and cover AAC, quiet AAC, additive-noise AAC, and streaming Opus variants. Negative cases include eight different generated songs, silence, and identical audio indexed under another track ID (ambiguity). Audio and databases are temporary.

Calibration uses seeds 1200–1203; held-out validation uses 2200–2203. Search observed top-candidate vote/support boundaries plus baseline values of 1 vote and 0.01 support. For each pair, require a positive gap strictly above all eligible incorrect candidates' margins and choose the largest gap retaining the most correct positives. Single-candidate margins use zero runner-up support. Keep zero incorrect matches, maximize correct positives, then break ties toward larger support, vote, and gap thresholds in that order. No useful safe separation returns no threshold candidate. Held-out seeds do not influence threshold selection.

| Set | Correct positives | Rejected negative/ambiguous cases | Incorrect matches |
| --- | --- | --- | --- |
| Calibration | 48 / 48 | 10 / 10 | 0 |
| Held-out | 47 / 48 | 10 / 10 | 0 |

The missed held-out clip is track 0 at 5.333 seconds with additive noise. Clean excerpts at every tested position pass. These small synthetic catalogs demonstrate mechanics and provide a reproducible starting point; they do not establish accuracy on real music, larger catalogs, room acoustics, or physical microphones. Peak settings and match thresholds still need task 6 evaluation.

## Task 6 offline evaluation

The [evaluation guide](evaluation.md) defines a labeled-clip manifest, song-level calibration/held-out separation, baseline and candidate reports, backend timing, and temporary SQLite backup measurements. It reuses the shared pipeline and calibration search without changing fingerprint generation or ±1-frame offset scoring. Threshold adoption requires calibration improvement, no observed incorrect matches or processing errors, and preservation of held-out baseline recognition. The tool only reports recommendations; current constants remain the synthetic baseline until representative real evidence is supplied. Profile experiments and re-indexing remain deferred.

## Expected failure cases

- The track is not in the indexed playlist catalog.
- The recording is too short, too quiet, or dominated by noise to produce enough stable peaks.
- A different recording/version is playing, or the audio is substantially changed in tempo, pitch, arrangement, or editing.
- Two candidates have similarly strong offset clusters.
- Catalog fingerprints were generated with a different algorithm profile than the query.

Weak evidence, no evidence, and ambiguous candidates map to **No match found**. Invalid input or processing failure is an error state, not a no-match result.

## References

- [Avery Li-Chun Wang, “An Industrial-Strength Audio Search Algorithm” (ISMIR 2003)](https://www.ee.columbia.edu/~dpwe/papers/Wang03-shazam.pdf) — primary description of sparse spectrogram constellations, paired hashes, and time-offset histogram scoring.
- [Cameron MacLeod, “How does Shazam work?”](https://www.cameronmacleod.com/blog/how-does-shazam-work) — explanation of the published pipeline and an Abracadabra implementation; notes that some target-zone details are implementation choices.
- [Abracadabra, pinned repository revision](https://github.com/notexactlyawe/abracadabra/tree/e0eb59a944d7c9999ff8a4bc53f5cfdeb07b39aa) — open-source Python reference implementation, not a product dependency or specification.
- [Khatri, Dillingham, and Chen, “Song Recognition Using Audio Fingerprinting” (2019)](https://hajim.rochester.edu/ece/sites/zduan/teaching/ece472/projects/2019/AudioFingerprinting.pdf) — source of the concrete initial STFT, six-band peak-selection, and 50-frame target-zone baseline.
- [Ana Harris, “How does Shazam work?”](https://medium.com/@anaharris/how-does-shazam-work-d38f74e41359) — introductory overview; used as supporting context rather than the algorithm specification.
