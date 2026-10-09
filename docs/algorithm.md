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

At this sample rate, a frame hop is 32 / 8,192 seconds (about 3.9 ms), and the initial 50-frame target zone spans about 195 ms. The maximum-filter neighborhood, peak density, quantization, and final thresholds still need measurement and tuning; the cited paper does not establish universal values for this project.

Record the selected profile/version in the catalog metadata. Recognition must reject an index made with a different profile or require it to be rebuilt before searching.

## Catalog indexing

For each authorized catalog audio file:

1. Decode the file, downmix to mono, and resample it using the shared profile.
2. Compute the STFT magnitude spectrogram.
3. Select local time-frequency maxima. Use the frequency bands and neighborhood filtering to retain prominent peaks with coverage throughout the track rather than a dense cluster in one region.
4. For each peak in time order, treat it as an anchor and pair it with every selected peak in the forward target zone. The bounded zone and sparse peak selection constrain the number of pairs.
5. Quantize the two frequency-bin values and their frame difference to produce each `hash_key`.
6. Store every key with the track ID and anchor frame. Keep available title, artist/channel, source URL, and the source audio path on the track record.

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

   Offset bins use STFT-frame units. Begin with a one-frame neighborhood when grouping offsets, then validate that tolerance on recorded clips.
7. Rank candidates by support ratio, using aligned-vote count as a minimum-evidence check and tie-breaker. Return the top track only if it clears calibrated vote and support thresholds and is not ambiguous with another candidate. Otherwise return **No match found**.

Each query-hash occurrence is counted at most once per candidate offset cluster so duplicate database postings do not inflate a score. The query audio and its temporary fingerprints are discarded after processing, including on errors.

## No-match threshold and evaluation

Do not choose a fixed vote count by intuition. Build a labeled evaluation set with:

- Positive clips from different positions in indexed tracks.
- Negative clips from tracks outside the catalog.
- Representative microphone recordings with noise, room acoustics, volume changes, and common compression.

Measure correct identification rate, false-positive rate, and processing time. Set the minimum aligned-vote and normalized-support thresholds (and any ambiguity margin) against positive and negative results, accounting for the size of the actual catalog. Wang's paper uses the raw count in the largest offset cluster as its score and recommends choosing a threshold from the distribution of the highest-scoring incorrect tracks. This MVP also normalizes by usable query hashes, as specified in the client brief. Until evaluation data exists, threshold values remain unset and results should not be presented as production-validated.

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
