# Magikazaam Client Brief

## Project overview

Magikazaam is a lightweight web app for identifying music playing nearby. A selected YouTube playlist supplies the catalog: an import script reads its tracks and adds authorized audio, metadata, and fingerprints to the database. A user taps **Identify Song**, records a short microphone clip, and searches the prepared catalog for the best supported match—or sees **No match found**.

The project recreates the public, Shazam-style audio fingerprinting method for a bounded catalog. It is not a copy of Shazam's commercial product or a claim to reproduce its private, current implementation.

## The problem

Finding a song by title is difficult when the listener does not know its lyrics or artist. Comparing raw recordings is also unreliable: a microphone adds room noise, changes volume, and captures only a short section that may begin anywhere in the track.

Magikazaam addresses this by comparing compact, repeatable patterns in the sound rather than comparing complete waveforms or relying on metadata embedded in the recording.

## Users and primary job

**Primary user:** someone listening to music from a playlist already added to the app's catalog.

**Primary job:** “When I hear a song I don't recognize, let me record a short sample and find out whether it is one of the songs in my catalog.”

## MVP experience

1. The user provides a selected YouTube playlist to the catalog import script.
2. The script reads available title, artist or channel, and source video URL metadata, obtains audio through an authorized method, and indexes each track.
3. The user taps **Identify Song** and grants microphone access if needed.
4. The app captures about 10 seconds, searches the prepared catalog, and displays the matched title and available artist or channel information.
5. If there is not enough aligned evidence for a reliable result, the app displays **No match found**.

The microphone sample is discarded after processing by default. The user does not need to create an account for this MVP.

## Recognition method

Use the published constellation-map and landmark-hash approach commonly associated with Shazam's 2003 paper:

1. **Prepare the audio consistently.** Decode each catalog file and microphone sample, downmix to mono, and resample to the same configured rate. Indexing and recognition must use identical signal-processing settings.
2. **Build a spectrogram.** Apply a windowed short-time Fourier transform (STFT) to overlapping audio frames and retain the magnitude information needed to find prominent time-frequency points.
3. **Select local peaks.** Pick strong local maxima across both time and frequency. Keep peaks distributed through the recording and across frequency regions so a short query can contain useful evidence even when noise affects part of the spectrum.
4. **Make landmark hashes.** For each peak, use it as an anchor and pair it with peaks in a bounded forward target zone. Encode the two quantized frequencies and their time difference as a hash, such as `(f_anchor, f_target, Δt)`. Store each hash with its track ID and anchor time.
5. **Search by hash.** For each query hash, use an indexed hash lookup to retrieve matching catalog postings. Do not compare every query frame against every full track.
6. **Check time alignment.** For each candidate posting, calculate `track_time − query_time`. Group votes for each track by this offset, allowing a small timing tolerance. A true match produces many hash votes clustered around one offset; scattered votes alone are not enough.
7. **Apply a no-match threshold.** Rank candidates by aligned support, normalized against the query's usable hashes. Return a result only when the leading candidate clears a threshold validated on both matching and non-matching clips; otherwise return **No match found**.

The Rochester project paper gives a reproducible starting profile (8,192 Hz mono audio, a 1,024-sample Hamming window, 32-sample hop, six logarithmic frequency bands, local maximum filtering, and a forward target zone of 50 frames). These are reference implementation choices, not universal Shazam constants. Treat them as a baseline to evaluate and tune against this project's audio, catalog size, and runtime.

The SQLite database stores track metadata, authorized catalog audio, and an inverted fingerprint lookup from hash to `(track_id, anchor_time)` postings. Keep the selected algorithm settings consistent between indexing and query processing; changing them requires re-indexing the catalog.

## Product requirements

- Index audio files from the selected playlist catalog.
- Identify songs from microphone clips captured at different positions in a track.
- Display a matched title and available artist or channel details.
- Return a clear no-match result for clips outside the catalog or without sufficient evidence.
- Discard microphone audio after processing by default; do not save recognition clips or query audio fingerprints as listening history.
- Track recognition accuracy, false matches, and processing time so thresholds can be evaluated instead of guessed.

## Success criteria

- Indexed songs are recognized from clips taken at different points, not only from the beginning.
- The selected result has a coherent cluster of matching hashes at one time offset.
- Unindexed songs and weak or ambiguous samples can produce **No match found** rather than an unsupported title.
- Results include the metadata available for the indexed track.
- Recognition accuracy, false-positive rate, and processing time can be measured on a representative set of matching and non-matching clips.

No numeric accuracy or latency target has been provided yet. Set those targets after an initial evaluation against the intended catalog and recording conditions.

## Scope and boundaries

**In scope:** a React web client, Python backend and audio processing, SQLite fingerprint catalog, playlist-based indexing, short microphone recognition, match and no-match results, and default disposal of query audio.

**Outside the MVP:** searching a global music catalog, lyrics or artist discovery, cover/remix identification, account features, saved listening history, an ML or LLM-based recognizer, and exact parity with Shazam's private production system. Alternate versions and substantial edits must be indexed as separate tracks.

Catalog audio must be supplied or obtained through authorized means. The app's initial recognition coverage is limited to tracks successfully indexed into its catalog.

## Technology

| Area | Choice |
|---|---|
| Web client | React |
| Backend and audio processing | Python |
| Track metadata, catalog audio, and fingerprint lookup | SQLite |

## Reference material

- [Cameron MacLeod — “How does Shazam work?”](https://www.cameronmacleod.com/blog/how-does-shazam-work): detailed explanation of the public landmark-hash pipeline, offset voting, and the Abracadabra implementation. The article notes that the commercial system may have evolved beyond the published method.
- [Ana Harris — “How does Shazam work?”](https://medium.com/@anaharris/how-does-shazam-work-d38f74e41359): introductory overview of audio fingerprints and the recognition flow.
- [Abracadabra, pinned repository revision](https://github.com/notexactlyawe/abracadabra/tree/e0eb59a944d7c9999ff8a4bc53f5cfdeb07b39aa): open-source Python implementation based on the published Shazam paper; useful as an implementation reference, not a drop-in product specification.
- [Khatri, Dillingham, and Chen — “Song Recognition Using Audio Fingerprinting” (2019)](https://hajim.rochester.edu/ece/sites/zduan/teaching/ece472/projects/2019/AudioFingerprinting.pdf): student implementation and evaluation describing a concrete STFT, peak-selection, hash, and offset-histogram baseline.
- [Wang — “An Industrial-Strength Audio Search Algorithm” (ISMIR 2003)](https://www.ee.columbia.edu/~dpwe/papers/Wang03-shazam.pdf): original public paper underlying the Shazam-style landmark-hash approach.
