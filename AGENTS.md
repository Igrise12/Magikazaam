# Repository Guidelines

## Project Structure & Module Organization

`src/magikazaam/` contains the CLI, FastAPI app, and shared playlist importer. `frontend/` contains the React/Vite client. `tests/` contains focused Python tests; `docs/` contains the product specifications. Audio preparation writes ignored staging files under `data/imports/`; the fingerprint pipeline and SQLite catalog remain planned work.

## Build, Test, and Development Commands

- `uv sync` — create the project environment and install declared dependencies.
- `uv run magikazaam` — start the local API on port 8001 with one worker.
- `uv run magikazaam import-playlist '<url-or-id>'` — prepare authorized playlist audio through the shared importer.
- `uv run pytest -q` — run backend tests; real decoding checks require FFmpeg/ffprobe.
- `uv build` — build the Python package.
- In `frontend/`, `npm ci`, `npm run dev`, and `npm run build` install, serve, and type-check/build the client.

## Coding Style & Naming Conventions

Prefer the smallest complete implementation that stays easy to read. Reuse existing code and dependencies; avoid extra layers, options, and abstractions until a real requirement needs them. Keep the code straightforward, but retain the validation and error handling needed for correct, safe behavior.

Use four spaces for Python indentation, `snake_case` for modules/functions/variables, and `CapWords` for classes. Add type annotations to public functions. For the planned React client, use `PascalCase` for components and `camelCase` for hooks and variables. Follow an existing formatter if one is added; none is configured currently.

## Testing Guidelines

Use pytest with `test_*.py` files under `tests/`; mock YouTube network access. No coverage target is set. As recognition is implemented, prioritize deterministic fingerprints, catalog transactions, offset matching, and no-match cases.

## Commit & Pull Request Guidelines

The repository has no commit history, so no commit convention is established. Use short imperative subjects, such as `Add playlist importer`. Pull requests should summarize user-visible changes, list verification performed, link a related issue when one exists, and include screenshots for frontend changes.

## Architecture & Agent Notes

Read `docs/client-brief.md`, `docs/architecture.md`, and `docs/algorithm.md` before changing product scope or recognition behavior. Catalog audio comes from a YouTube playlist through an authorized acquisition method and is stored in SQLite; microphone query audio is temporary. For code-location work, use CodeGraph before searching or reading code when a `.codegraph/` directory exists at the repository root; otherwise use `rg`.
