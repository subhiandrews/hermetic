# Hermetic

Hermetic is an open-source, agentic tester for REST microservices. Give it an OpenAPI spec and any design or architecture docs. It works out the service's external dependencies (databases, caches, downstream HTTP services), stands them up as mocks, generates a runnable test suite, runs it, and reports failures with a `curl` command you can paste to reproduce each one.

Each run is kept in a local history. A small web UI shows past runs, which tests failed and why, and which tests were added, modified, or dropped since the last run, and why.

> **Status:** pre-alpha. The scaffold exists; features land milestone by milestone. See `docs/architecture.md`.

## Install

```bash
pip install hermetic          # once published
# or run the container
docker run --rm ghcr.io/subhiandrews/hermetic --help
```

## Develop

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12+.

```bash
uv sync --group dev
uv run pytest --cov
uv run ruff check . && uv run ruff format --check . && uv run mypy
uv run hermetic version
```

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT. Contributions are welcome; improvements are shared back through pull requests.
