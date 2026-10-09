# Project Brief: Playlist-Based Song Recognition App

## Overview

Build a lightweight web app that identifies a song from a short microphone recording. The app will create audio fingerprints from tracks in a selected YouTube playlist, then compare a recorded clip against those fingerprints using the Shazam-style method discussed: spectrograms, peaks, paired hashes, and time-alignment matching.

## Song Catalog

The initial catalog will use audio files obtained from a selected YouTube playlist through authorized download methods. The app will index those files before they can be recognized.

For each track, store available metadata such as its title, artist or channel, and source video URL. The MVP will search this prepared catalog.

## Recognition Algorithm

### Indexing playlist tracks

For each audio file:

1. Generate a spectrogram by analyzing short audio sections with a Fourier transform.
2. Find strong local peaks in the spectrogram.
3. Pair each anchor peak with peaks in a nearby target zone.
4. Create a hash from the two peak frequencies and their time difference: `(frequency A, frequency B, Δt)`.
5. Store each hash with the track ID and the anchor's position in the track.

### Recognizing a recording

1. Capture approximately 10 seconds of audio from the microphone.
2. Generate hashes from the recording using the same process.
3. Look up those hashes in the SQLite fingerprint index.
4. Group matches by track and calculate `track time − recording time` for each match.
5. Build a vote count for each track and time offset. A strong group of hashes with the same offset indicates a likely match.
6. Return the best match if its score passes a threshold; otherwise, show **“No match found.”**

## User Flow

1. Add audio files from the selected playlist to the catalog.
2. The app generates and stores fingerprints for those tracks.
3. The user taps **Identify Song** and allows microphone access.
4. The app records a short clip, searches the fingerprint index, and displays the result.

## Tech Stack

| Component | Technology |
|---|---|
| Frontend | React |
| Backend and audio processing | Python |
| Database | SQLite |

The initial algorithm does not require a trained ML model or an LLM. Its core is audio signal processing and database matching.

## MVP Requirements

- Index audio files from the selected playlist.
- Recognize a song from microphone audio.
- Display the matched title and available artist or channel information.
- Show a clear no-match result.
- Discard the microphone recording after processing by default.

## Success Criteria

- Recognize indexed songs from clips taken at different points in each track.
- Return no match when the clip does not sufficiently match the catalog.
- Track recognition accuracy, false matches, and processing time.

## MVP Scope

Recognition is limited to tracks that have been added to the playlist catalog. Alternate versions, remixes, or recordings with substantial changes may need to be indexed separately.

## Reference

- [How does Shazam work?](https://www.cameronmacleod.com/blog/how-does-shazam-work)
