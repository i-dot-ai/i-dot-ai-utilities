# `i-dot-ai-utilities` Contributing Guide

## How to use uv

#### Docs can be found here -> https://docs.astral.sh/uv/

`uv` is a python version and package manager that takes features similar to those you'd find in poetry, pyenv and virtualenv,
and handles them all in one tool. It's built using Rust by Astral (same creators as `ruff`), so it runs far faster than comparable tools.

Install it from [here](https://formulae.brew.sh/formula/uv) using brew. Be sure to change your install location from the default applications location.

Update `uv` itself to latest with `uv self update`.

Initialize `uv` into `.venv` by running `uv venv`.
This venv will install all packages and the python version listed in the pyproject.toml file.
This is the venv that will be used for this project only, no package or python versions need managing externally with `uv`.

Active the venv with `source .venv/bin/activate`.

Install packages from `pyproject.toml` using `uv pip install -e`.

You can also activate and install packages to the venv by using `uv sync` in one command.

To specify a particular python version you can use `uv venv --python 3.11` if needed.

To regenerate the lockfile (`uv.lock`), you can use `uv lock`.

Dependencies can be added with the following:

``` bash
uv add fastapi uvicorn         # Production dependencies
uv add pytest black --dev      # Development dependencies
```

Scripts or tools can be run with the following:

``` bash
uv run python scratch.py      # Running a local script
uv run ruff format            # Running ruff format
uv run ruff check --fix       # Running ruff check
```
