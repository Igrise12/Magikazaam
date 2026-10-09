"""Persistent source audio and landmark postings for the local catalog."""

from collections.abc import Iterator
from contextlib import contextmanager
from itertools import chain
from pathlib import Path
import sqlite3

from magikazaam.fingerprint import FingerprintFailure, check_fingerprint_profile, fingerprint_audio, fingerprint_profile
from magikazaam.importer import ImportFailure, TrackResult, VIDEO_ID


CATALOG_PATH = Path("data/catalog.sqlite3")


class CatalogFailure(ImportFailure):
    """A catalog error safe to display to the user."""


def check_profile(connection: sqlite3.Connection) -> None:
    """Require the current profile before reading or writing an index."""
    try:
        row = connection.execute("SELECT profile FROM catalog_meta WHERE id = 1").fetchone()
    except sqlite3.Error as exc:
        raise CatalogFailure("Catalog metadata is missing or unreadable. Rebuild the catalog before using it.") from exc
    try:
        check_fingerprint_profile(row[0] if row else None)
    except FingerprintFailure as exc:
        raise CatalogFailure(str(exc)) from exc


@contextmanager
def open_catalog(path: Path = CATALOG_PATH, *, initialize: bool = False) -> Iterator[sqlite3.Connection]:
    """Open a connection in its caller's thread, initializing only a new database."""
    connection = None
    try:
        existed = path.exists()
        if initialize:
            path.parent.mkdir(parents=True, exist_ok=True)
        mode = "rwc" if initialize else "ro"
        connection = sqlite3.connect(f"{path.absolute().as_uri()}?mode={mode}", uri=True, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        if initialize:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                tables = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
                if not existed and not tables:
                    connection.execute("""CREATE TABLE tracks (
                        video_id TEXT PRIMARY KEY NOT NULL,
                        title TEXT NOT NULL, channel TEXT, source_url TEXT NOT NULL,
                        audio_blob BLOB NOT NULL,
                        audio_format TEXT NOT NULL CHECK (audio_format IN ('.m4a', '.webm'))
                    )""")
                    connection.execute("""CREATE TABLE fingerprints (
                        hash_key INTEGER NOT NULL,
                        track_id TEXT NOT NULL REFERENCES tracks(video_id) ON DELETE CASCADE,
                        anchor_frame INTEGER NOT NULL CHECK (anchor_frame >= 0)
                    )""")
                    connection.execute("CREATE INDEX fingerprints_hash ON fingerprints(hash_key)")
                    connection.execute("CREATE TABLE catalog_meta (id INTEGER PRIMARY KEY CHECK (id = 1), profile TEXT NOT NULL)")
                    connection.execute("INSERT INTO catalog_meta VALUES (1, ?)", (fingerprint_profile(),))
                check_profile(connection)
        else:
            check_profile(connection)
        yield connection
    except (OSError, sqlite3.Error) as exc:
        raise CatalogFailure("Could not access the catalog. Check disk space, database permissions, and other running imports.") from exc
    finally:
        if connection is not None:
            connection.close()


def encode_key(key: tuple[int, int, int]) -> int:
    """Pack two 10-bit frequency bins and a 6-bit frame delta without collisions."""
    anchor, target, delta = key
    if not (1 <= anchor <= 512 and 1 <= target <= 512 and 1 <= delta <= 50):
        raise CatalogFailure("Fingerprint key is outside the supported profile ranges.")
    return (anchor << 16) | (target << 6) | delta


def has_track(connection: sqlite3.Connection, video_id: str) -> bool:
    return connection.execute("SELECT 1 FROM tracks WHERE video_id = ?", (video_id,)).fetchone() is not None


def index_track(connection: sqlite3.Connection, track: TrackResult, audio: Path) -> bool:
    """Atomically store one track; return False when another import already stored it."""
    if not isinstance(track.video_id, str) or not VIDEO_ID.fullmatch(track.video_id):
        raise CatalogFailure("This track has an invalid YouTube video ID.")
    if not isinstance(track.title, str) or not track.title.strip() or (track.channel is not None and not isinstance(track.channel, str)):
        raise CatalogFailure("This track has invalid title or channel metadata.")
    try:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            check_profile(connection)
            if has_track(connection, track.video_id):
                return False
            postings = iter(fingerprint_audio(audio))
            first = next(postings, None)
            if first is None:
                raise CatalogFailure("Audio produced no fingerprints. Try a track with audible music.")
            connection.execute(
                "INSERT INTO tracks VALUES (?, ?, ?, ?, ?, ?)",
                (track.video_id, track.title, track.channel,
                 f"https://www.youtube.com/watch?v={track.video_id}", audio.read_bytes(), audio.suffix),
            )
            connection.executemany(
                "INSERT INTO fingerprints VALUES (?, ?, ?)",
                ((encode_key(key), track.video_id, frame) for key, frame in chain((first,), postings)),
            )
    except FingerprintFailure as exc:
        raise CatalogFailure(str(exc)) from exc
    except (OSError, sqlite3.Error) as exc:
        raise CatalogFailure("Could not index this track. Check disk space, database permissions, and other running imports.") from exc
    return True


def list_tracks(path: Path = CATALOG_PATH) -> list[dict]:
    """Return only catalog metadata; a catalog that does not exist is empty."""
    if not path.exists():
        return []
    with open_catalog(path) as connection:
        return [dict(row) for row in connection.execute(
            "SELECT video_id, title, channel, source_url FROM tracks ORDER BY title COLLATE NOCASE, video_id"
        )]
