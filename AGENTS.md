# Repository Guidelines

## Project Structure & Module Organization

`src/magikazaam/` is the Python package. `pyproject.toml` defines the package and its `magikazaam` command. `docs/` contains the client brief, architecture, recognition algorithm, and project notes. The React frontend, playlist importer, database layer, tests, and static assets are planned work; they are not present yet.

## Build, Test, and Development Commands

- `uv sync` — create the project environment and install declared dependencies.
- `uv run magikazaam` — run the configured console entry point.
- `uv build` — build the Python package.

There is no frontend package manifest, test runner, or lint/format configuration yet. Add the relevant commands to `pyproject.toml` or the frontend manifest when those parts are introduced.

## Coding Style & Naming Conventions

Prefer the smallest complete implementation that stays easy to read. Reuse existing code and dependencies; avoid extra layers, options, and abstractions until a real requirement needs them. Keep the code straightforward, but retain the validation and error handling needed for correct, safe behavior.

Use four spaces for Python indentation, `snake_case` for modules/functions/variables, and `CapWords` for classes. Add type annotations to public functions. For the planned React client, use `PascalCase` for components and `camelCase` for hooks and variables. Follow an existing formatter if one is added; none is configured currently.

## Testing Guidelines

No tests or test framework exist yet, and no coverage target is set. Add focused tests under `tests/` as behavior is implemented; use `test_*.py` names for Python tests. Prioritize deterministic fingerprint output, catalog transaction behavior, offset matching, and no-match cases. Document the chosen test command when a runner is added.

## Commit & Pull Request Guidelines

The repository has no commit history, so no commit convention is established. Use short imperative subjects, such as `Add playlist importer`. Pull requests should summarize user-visible changes, list verification performed, link a related issue when one exists, and include screenshots for frontend changes.

## Architecture & Agent Notes

Read `docs/client-brief.md`, `docs/architecture.md`, and `docs/algorithm.md` before changing product scope or recognition behavior. Catalog audio comes from a YouTube playlist through an authorized acquisition method and is stored in SQLite; microphone query audio is temporary. For code-location work, use CodeGraph before searching or reading code when a `.codegraph/` directory exists at the repository root; otherwise use `rg`.
