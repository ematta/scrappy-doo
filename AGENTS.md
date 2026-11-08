# Repository Guidelines

## Project Structure & Module Organization

This is an early-stage Python package using the `src` layout. Application code lives in `src/scrappy_doo/`; `__init__.py` currently defines the `main()` entry point. Project metadata, dependencies, and the `scrappy-doo` command are declared in `pyproject.toml`. `README.md` is available for project-facing documentation. There are currently no test or asset directories.

Keep new package modules under `src/scrappy_doo/`. Place unit tests in a top-level `tests/` directory as the project grows, and keep test fixtures close to the tests that use them.

## Build, Test, and Development Commands

- `uv sync` creates or updates the project environment using the Python version in `.python-version` (3.13).
- `uv run scrappy-doo` runs the configured command-line entry point.
- `uv build` builds distribution artifacts using the configured `uv_build` backend.

No test runner, linting tool, or coverage threshold is configured yet. Add and document the appropriate commands when introducing those tools.

## Coding Style & Naming Conventions

Follow standard Python conventions: use four spaces for indentation, `snake_case` for modules and functions, and `PascalCase` for classes. Match the existing type annotations where useful. Prefer the standard library and keep external dependencies limited; declare any necessary dependencies in `pyproject.toml`.

## Testing Guidelines

Write unit tests first for behavioral changes and ask the project owner whether the proposed coverage is comprehensive enough. Name test files `test_<module>.py` and test functions `test_<behavior>`. Select and configure a test framework when tests are introduced. The project owner handles end-to-end tests and may collaborate on diagnosing them.

## Commits & Pull Requests

The repository has no commit history yet, so no established message convention can be inferred. Use concise, imperative commit subjects that describe the change. Pull requests should explain the change and its motivation, list verification performed, and link any relevant issue. Include screenshots when a change has a visual effect.
