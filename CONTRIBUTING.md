# `i-dot-ai-utilities` contributing guide

## Use uv

`uv` is a Python version and package manager built in Rust by Astral (the creators of `ruff`). It replaces Poetry, pyenv, and virtualenv in a single tool. Read the [uv documentation](https://docs.astral.sh/uv/) for full command reference.

Install `uv` using the [uv Homebrew formula](https://formulae.brew.sh/formula/uv). Change the install location if you do not want the default Homebrew prefix.

Update `uv` to the latest release:

```bash
uv self update
```

Create a project virtual environment in `.venv`:

```bash
uv venv
```

To target a specific Python version, pass `--python`:

```bash
uv venv --python 3.11
```

Activate the virtual environment:

```bash
source .venv/bin/activate
```

Install the package and dependencies from `pyproject.toml` in editable mode:

```bash
uv pip install -e .
```

To create the virtual environment and install locked dependencies in one command, run:

```bash
uv sync
```

Regenerate the lockfile (`uv.lock`) after editing dependencies:

```bash
uv lock
```

Add production or development dependencies:

```bash
uv add fastapi uvicorn      # Production dependencies
uv add pytest ruff --dev    # Development dependencies
```

Run scripts or tools inside the project environment:

```bash
uv run python scratch.py    # Run a local script
uv run ruff format          # Format code with ruff
uv run ruff check --fix     # Lint and auto-fix with ruff
```

## Run tests and linters

Use the `Makefile` targets to test and lint changes before opening a pull request:

```bash
make test           # Start Docker Compose backing services and run all module test suites
make test-logging   # Run only the logging module tests (no backing services required)
make test-otel      # Run only the otel module tests (no backing services required)
make lint           # Run ruff format, ruff check --fix, mypy, and bandit
```
