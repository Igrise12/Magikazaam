"""Import authorized playlist audio into the persistent fingerprint catalog."""

from collections.abc import Callable
from dataclasses import asdict, dataclass, field
import json
import logging
import math
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

MAX_TRACKS = 100
MAX_DURATION = 15 * 60
MAX_BYTES = 50 * 1024 * 1024
STAGING_ROOT = Path("data/imports")
VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}\Z")
PLAYLIST_ID = re.compile(r"[A-Za-z0-9_-]{10,128}\Z")


class ImportFailure(ValueError):
    """A useful error safe to display to the importer user."""


class AudioProcessingFailure(ImportFailure):
    """Audio tools are unavailable or did not finish processing."""


@dataclass
class TrackResult:
    video_id: str | None
    title: str
    channel: str | None
    source_url: str | None
    status: str = "failed"
    error: str | None = None


@dataclass
class ImportResult:
    playlist: str
    import_id: str = field(default_factory=lambda: uuid4().hex)
    state: str = "queued"
    total: int = 0
    completed: int = 0
    tracks: list[TrackResult] = field(default_factory=list)
    error: str | None = None


def normalize_playlist(value: str) -> str:
    """Accept a playlist ID or YouTube playlist/watch URL, never arbitrary URLs."""
    value = value.strip()
    if PLAYLIST_ID.fullmatch(value):
        playlist_id = value
    else:
        try:
            url = urlsplit(value)
            ids = parse_qs(url.query).get("list", [])
            valid_url = (
                url.scheme in {"https", "http"}
                and url.hostname in {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}
                and url.path in {"/playlist", "/watch"}
                and url.port is None
                and url.username is None
                and url.password is None
                and len(ids) == 1
            )
        except ValueError:
            valid_url = False
        if not valid_url or not PLAYLIST_ID.fullmatch(ids[0]):
            raise ImportFailure("Enter a YouTube playlist URL or playlist ID.")
        playlist_id = ids[0]
    return f"https://www.youtube.com/playlist?list={playlist_id}"


def check_prerequisites() -> None:
    for binary in ("ffmpeg", "ffprobe", "node"):
        if shutil.which(binary) is None:
            raise ImportFailure(f"Install {binary} and make it available on PATH before importing.")


def validate_audio(path: Path, *, max_duration: float | None = None, allow_missing_duration: bool = False) -> None:
    """Check actual content and decode the complete permitted track without saving PCM."""
    if path.suffix not in {".m4a", ".webm"} or not path.is_file() or path.is_symlink():
        raise ImportFailure("Only M4A/AAC or WebM/Opus audio files are supported.")
    if not 0 < path.stat().st_size <= MAX_BYTES:
        raise ImportFailure("Audio must be nonempty and at most 50 MiB.")
    limit = MAX_DURATION if max_duration is None else max_duration
    duration_message = f"Audio must have a positive duration of at most {limit:g} seconds."
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-protocol_whitelist", "file,pipe", "-show_entries",
             "stream=codec_type,codec_name:format=duration,format_name", "-of", "json", str(path)],
            capture_output=True, text=True, check=True, timeout=30,
        )
        info = json.loads(probe.stdout)
        streams = info.get("streams", [])
        expected_codec = "aac" if path.suffix == ".m4a" else "opus"
        if len(streams) != 1 or streams[0].get("codec_type") != "audio" or streams[0].get("codec_name") != expected_codec:
            raise ImportFailure("The file must contain only AAC (M4A) or Opus (WebM) audio.")
        if allow_missing_duration:
            formats = set(info.get("format", {}).get("format_name", "").split(","))
            expected_format = "webm" if path.suffix == ".webm" else "mp4"
            if expected_format not in formats:
                raise ImportFailure("The recording must use a WebM/Opus or MP4/AAC container.")
        duration = info.get("format", {}).get("duration")
        if duration is None and not allow_missing_duration:
            raise ImportFailure(duration_message)
        if duration is not None and (not math.isfinite(float(duration)) or not 0 < float(duration) <= limit):
            raise ImportFailure(duration_message)
        decoded = subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "error", "-xerror", "-protocol_whitelist", "file,pipe",
             "-i", str(path), "-map", "0:a:0", "-t", str(limit + 1),
             "-progress", "pipe:1", "-f", "null", "-"],
            capture_output=True, text=True, check=True, timeout=120,
        )
        times = re.findall(r"^out_time_us=(\d+)$", decoded.stdout, re.MULTILINE)
        if not times or not 0 < int(times[-1]) <= limit * 1_000_000:
            raise ImportFailure(duration_message)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AudioProcessingFailure("Audio could not be decoded. Check FFmpeg/ffprobe and file permissions, then retry.") from exc
    except (subprocess.CalledProcessError, KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, ImportFailure):
            raise
        raise ImportFailure("Audio could not be decoded or its duration could not be verified.") from exc


def _download_progress(progress: dict) -> None:
    if progress.get("downloaded_bytes", 0) > MAX_BYTES:
        raise ImportFailure("Audio exceeds the 50 MiB limit.")


def _duration_filter(info: dict, *, incomplete: bool = False) -> str | None:
    duration = info.get("duration")
    if duration is not None and (not math.isfinite(duration) or not 0 < duration <= MAX_DURATION):
        return "Track exceeds the 15-minute limit or has an invalid duration."
    if info.get("is_live") or info.get("live_status") in {"is_live", "is_upcoming"}:
        return "Live and upcoming streams are not supported."
    return None


def import_playlist(
    result: ImportResult,
    staging_root: Path = STAGING_ROOT,
    on_progress: Callable[[ImportResult], None] | None = None,
    *,
    catalog_path: Path = Path("data/catalog.sqlite3"),
) -> ImportResult:
    """Shared CLI/API importer; successful tracks are committed to SQLite."""
    # Local import avoids the fingerprint decoder's dependency on audio validation here.
    from magikazaam.catalog import has_track, index_track, open_catalog
    result.playlist = normalize_playlist(result.playlist)
    check_prerequisites()
    result.state = "running"
    job_dir = staging_root / result.import_id
    job_dir.mkdir(parents=True, exist_ok=False)

    def publish() -> None:
        manifest = job_dir / "manifest.json"
        temporary_manifest = job_dir / "manifest.json.tmp"
        try:
            temporary_manifest.write_text(json.dumps(asdict(result), indent=2), encoding="utf-8")
            temporary_manifest.replace(manifest)
        except OSError as exc:
            raise ImportFailure("Could not write the import manifest. Check disk space and directory permissions.") from exc
        if on_progress:
            on_progress(result)

    options = {
        "quiet": True, "no_warnings": True, "js_runtimes": {"node": {}},
        "socket_timeout": 20, "retries": 2, "fragment_retries": 2,
    }
    try:
        with open_catalog(catalog_path, initialize=True):
            pass
        publish()
        with YoutubeDL({**options, "extract_flat": "in_playlist", "playlistend": MAX_TRACKS + 1}) as ydl:
            try:
                playlist = ydl.extract_info(result.playlist, download=False)
            except DownloadError as exc:
                raise ImportFailure("Playlist is unavailable or requires login. Check its visibility and URL.") from exc
        if not playlist or "entries" not in playlist:
            raise ImportFailure("No accessible playlist entries were found.")
        entries = list(playlist["entries"])
        if len(entries) > MAX_TRACKS or (playlist.get("playlist_count") or 0) > MAX_TRACKS:
            raise ImportFailure("Playlist exceeds the 100-entry limit; use a smaller playlist.")
        if not entries:
            raise ImportFailure("The playlist is empty or unavailable.")
        result.total = len(entries)
        publish()
        for entry in entries:
            entry = entry or {}
            video_id = entry.get("id")
            valid_id = isinstance(video_id, str) and VIDEO_ID.fullmatch(video_id)
            track = TrackResult(
                video_id=video_id if valid_id else None,
                title=entry.get("title") or "Unavailable track",
                channel=entry.get("channel") or entry.get("uploader"),
                source_url=f"https://www.youtube.com/watch?v={video_id}" if valid_id else None,
            )
            with tempfile.TemporaryDirectory(prefix=".partial-", dir=job_dir) as temporary:
                work_dir = Path(temporary)
                try:
                    if not valid_id:
                        raise ImportFailure("This playlist entry is unavailable.")
                    with open_catalog(catalog_path) as catalog:
                        existing = has_track(catalog, video_id)
                    if existing:
                        track.status = "skipped"
                    else:
                        download_options = {
                            **options, "noplaylist": True,
                            "format": "bestaudio[ext=m4a][acodec^=mp4a]/bestaudio[ext=webm][acodec=opus]",
                            "outtmpl": str(work_dir / "%(id)s.%(ext)s"),
                            "max_filesize": MAX_BYTES, "match_filter": _duration_filter,
                            "progress_hooks": [_download_progress],
                        }
                        with YoutubeDL(download_options) as ydl:
                            info = ydl.extract_info(track.source_url, download=True)
                        if not info:
                            raise ImportFailure("Track is unavailable, live, or exceeds the 15-minute limit.")
                        track.title = info.get("title") or track.title
                        track.channel = info.get("channel") or info.get("uploader") or track.channel
                        files = list(work_dir.iterdir())
                        if len(files) != 1:
                            raise ImportFailure("No complete supported audio file was downloaded; check format and size limits.")
                        audio = files[0]
                        validate_audio(audio)
                        with open_catalog(catalog_path, initialize=True) as catalog:
                            track.status = "indexed" if index_track(catalog, track, audio) else "skipped"
                except DownloadError:
                    track.error = "Download failed. The track may be restricted, unavailable, or unsupported."
                except ImportFailure as exc:
                    track.error = str(exc)
                except OSError:
                    track.error = "Could not process temporary audio. Check disk space and staging directory permissions."
            result.tracks.append(track)
            result.completed += 1
            publish()
        result.state = "completed"
    except ImportFailure as exc:
        result.state = "failed"
        result.error = str(exc)
    except Exception:
        logging.exception("Playlist import failed")
        result.state = "failed"
        result.error = "Import could not finish. Check the server and try again."
    finally:
        publish()
    return result
