"""Local, single-worker HTTP API for playlist indexing."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import asdict
import logging
from pathlib import Path
import tempfile
from threading import Lock

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect

from magikazaam.catalog import CATALOG_PATH, CatalogFailure, list_tracks
from magikazaam.fingerprint import FingerprintFailure, FingerprintProcessingFailure
from magikazaam.importer import ImportFailure, ImportResult, STAGING_ROOT, normalize_playlist, import_playlist
from magikazaam.recognition import MAX_QUERY_BYTES, identify_audio


class ImportRequest(BaseModel):
    playlist: str = Field(min_length=1, max_length=2048)


def create_app(staging_root: Path = STAGING_ROOT, catalog_path: Path = CATALOG_PATH) -> FastAPI:
    jobs: dict[str, dict] = {}
    lock = Lock()
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="playlist-import")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        executor.shutdown(wait=True)

    app = FastAPI(title="Magikazaam", lifespan=lifespan)

    @app.post("/api/identify")
    async def identify(request: Request) -> dict:
        media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        suffix = {"audio/webm": ".webm", "audio/mp4": ".m4a"}.get(media_type)
        if suffix is None:
            raise HTTPException(status_code=415, detail="Record WebM/Opus or MP4/AAC audio in a supported browser.")
        try:
            with tempfile.TemporaryDirectory(prefix="magikazaam-query-") as temporary:
                audio = Path(temporary) / f"query{suffix}"
                size = 0
                with audio.open("wb") as output:
                    async for chunk in request.stream():
                        size += len(chunk)
                        if size > MAX_QUERY_BYTES:
                            raise HTTPException(status_code=413, detail="Recording must be at most 5 MiB.")
                        output.write(chunk)
                if not size:
                    raise HTTPException(status_code=422, detail="Recording is empty. Check your microphone and retry.")
                worker = asyncio.create_task(run_in_threadpool(identify_audio, audio, catalog_path))
                try:
                    return await asyncio.shield(worker)
                except asyncio.CancelledError:
                    # A cancelled HTTP task must not remove a file still being decoded by its worker.
                    while not worker.done():
                        try:
                            await asyncio.shield(worker)
                        except asyncio.CancelledError:
                            continue
                        except Exception:
                            break
                    if not worker.cancelled():
                        worker.exception()
                    raise
        except ClientDisconnect as exc:
            raise HTTPException(status_code=400, detail="Recording upload was interrupted. Try again.") from exc
        except CatalogFailure as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except FingerprintProcessingFailure as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except FingerprintFailure as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except HTTPException:
            raise
        except Exception as exc:
            # Do not log clip contents, filenames, or query fingerprints.
            raise HTTPException(status_code=500, detail="Recognition could not finish. Check the server and try again.") from exc

    def publish(result: ImportResult) -> None:
        snapshot = asdict(result)
        with lock:
            jobs[result.import_id] = snapshot

    def run(result: ImportResult) -> None:
        try:
            import_playlist(result, staging_root=staging_root, on_progress=publish, catalog_path=catalog_path)
        except ImportFailure as exc:
            result.state, result.error = "failed", str(exc)
        except Exception:
            logging.exception("Playlist import failed")
            result.state = "failed"
            result.error = "Import could not finish. Check the server and try again."
        finally:
            publish(result)

    @app.get("/api/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.get("/api/catalog")
    def catalog_tracks() -> list[dict]:
        try:
            return list_tracks(catalog_path)
        except CatalogFailure as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/imports", status_code=202)
    def start_import(request: ImportRequest) -> dict:
        try:
            playlist = normalize_playlist(request.playlist)
        except ImportFailure as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        result = ImportResult(playlist=playlist)
        with lock:
            if any(job["state"] in {"queued", "running"} for job in jobs.values()):
                raise HTTPException(status_code=409, detail="An import is already running. Wait for it to finish.")
            # shortcut: job status is in memory; add persistence when restart recovery is needed.
            jobs[result.import_id] = asdict(result)
            executor.submit(run, result)
        return {"import_id": result.import_id}

    @app.get("/api/imports/{import_id}")
    def import_status(import_id: str) -> dict:
        with lock:
            if import_id not in jobs:
                raise HTTPException(status_code=404, detail="Import not found. Server restarts clear import status.")
            return jobs[import_id]

    return app


app = create_app()
