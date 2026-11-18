# Postpilot

Postpilot turns GitHub activity and Ukrainian Telegram drafts into social posts
and avatar videos. Every post requires approval in Telegram before publishing.

See [project documentation](docs/) for the spec, architecture, and task plan.

## Local checks

With Python 3.12 or later, create and activate a virtual environment, then run:

```sh
python -m pip install -e '.[dev]'
pre-commit install
ruff check .
ruff format --check .
mypy src
pytest
python scripts/scan_secrets.py
```

Run `postpilot` to see the version and planned commands.
