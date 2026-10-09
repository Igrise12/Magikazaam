"""Console entry point and shared playlist indexing command."""

import argparse
import json
from dataclasses import asdict

from magikazaam.importer import ImportFailure, ImportResult, normalize_playlist, import_playlist


def main() -> None:
    parser = argparse.ArgumentParser(description="Index a playlist catalog for Magikazaam.")
    commands = parser.add_subparsers(dest="command")
    importer = commands.add_parser("import-playlist", help="Index authorized audio from a public/unlisted playlist")
    importer.add_argument("playlist", help="YouTube playlist URL or ID")
    args = parser.parse_args()
    if args.command == "import-playlist":
        try:
            result = import_playlist(ImportResult(playlist=normalize_playlist(args.playlist)))
        except (ImportFailure, OSError) as exc:
            parser.exit(1, f"Import failed: {exc}\n")
        print(json.dumps(asdict(result), indent=2))
        raise SystemExit(1 if result.state == "failed" or any(track.status == "failed" for track in result.tracks) else 0)
    import uvicorn

    uvicorn.run("magikazaam.api:app", host="127.0.0.1", port=8001, workers=1)
