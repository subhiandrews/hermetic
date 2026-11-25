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

## Use

```bash
hermetic discover openapi.yaml --docs docs/       # finds the databases, caches and services it depends on
hermetic compose --service <service>             # writes the mock stack (compose file) for those dependencies
hermetic stubs --service <service> --name <dep> --spec dep.yaml   # WireMock stubs for an HTTP dependency, from its OpenAPI file
hermetic up --service <service>                   # starts it and prints NAME=URL lines; `hermetic down` removes it
hermetic generate openapi.yaml --docs docs/      # writes tests/<service>/ from the spec
hermetic run --target http://localhost:8080 --spec openapi.yaml   # runs it and records the run
hermetic serve                                   # the web UI, at http://127.0.0.1:8000
hermetic export --service <service>              # packs the tests into hermetic-tests-<service>.zip
hermetic publish --service <service>             # hands the tests to a publisher (local, or --publisher github-pr)
```

`<service>` is a slug of the spec's title unless you pass `--service`. Each command has `--help`.

### Web UI

`hermetic serve` lists the services, each service's runs with a pass-rate trend, and every run's failures (with a `curl` to reproduce) and added, modified and dropped tests. It listens on loopback only and refuses a `Host` header it was not started for.

In a container the server has to listen on every interface for the published port to reach it. Publish that port on loopback only, and mount the history so it outlives the container:

```bash
docker run --rm -p 127.0.0.1:8000:8000 -v "$PWD/.hermetic:/work/.hermetic" \
  ghcr.io/subhiandrews/hermetic serve --host 0.0.0.0
```

It then answers to `localhost`. For any other name (a reverse proxy, a team hostname) add `--allowed-host <name>`; without it that name gets a `421`. `--host 0.0.0.0` prints a warning because anyone who can reach the port can read the history.

To keep one history for many machines, set `HERMETIC_API_TOKEN` (32 or more characters) on the server and `hermetic run --history-dir https://your-server` on each client, which reads the same variable. Put a TLS proxy in front of it. See `docs/architecture.md` (Central history).

## Examples

[`examples/petclinic`](examples/petclinic): a real OpenAPI spec (Spring PetClinic REST) with a design note and the files Hermetic produces from them.

[`examples/sockshop`](examples/sockshop): the Sock Shop orders service, which has a database and calls three other services, all described by Swagger 2.0 files.

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
