# Hermetic architecture

Hermetic tests a REST microservice from its OpenAPI spec and design docs. Deterministic code does parsing, diffing, execution, recording and reporting. A Claude Agent SDK agent only infers dependencies and writes tests, which keeps runs reproducible and the tool testable.

## Pipeline

```
hermetic run --spec openapi.yaml --docs ./docs --target http://svc:8080
  1. Ingest      parse OpenAPI plus Markdown/ADR docs into a ServiceModel
  2. Discover    agent infers dependencies (DB, cache, queue, downstream HTTP)
  3. Provision   generate docker-compose.hermetic.yml: lightweight engines plus WireMock/Prism
  4. Generate    agent writes or updates a pytest + httpx suite
  5. Diff        compare spec/doc hashes against the test manifest; regenerate only what changed
  6. Execute     run the suite against the target; capture request/response
  7. Record      write the run to the run-history store
  8. Report      Markdown / HTML / JSON summary, with a curl repro per failure
  9. Publish     optional pluggable sinks (interface only in v1)
```

## Ingest

`hermetic.ingest` turns an OpenAPI 3.x file (JSON or YAML) and an optional docs directory into a frozen `ServiceModel`. These are the decisions later stages depend on. Exact limits live as constants in `ingest/openapi.py` and `ingest/docs.py`.

- **The spec is untrusted input.** It is parsed into plain JSON types (dates and integer keys are converted), then validated. Ambiguous YAML (an unquoted `on` key), duplicate keys and values JSON cannot represent are errors. File size, node counts and the expansion of references are bounded, so a hostile or pathological file fails with a clear error instead of exhausting memory.
- **Only local `$ref`s (`#/...`) are resolved.** External ones are rejected before the validator runs, because its resolver would otherwise fetch URLs or read files. In 3.1 documents, keywords next to a `$ref` are merged over its target; 3.0 ignores them, as that version specifies. Known limitation: any object with a string `$ref` key is read as a reference, including literal data inside an `example` or `default`; telling schema positions from data positions needs a position-aware walk, which is not done yet.
- **Recursive schemas keep a reference.** A `$ref` back into a schema that is already being expanded is left as `{"$ref": "#/components/schemas/Name"}`. The model carries no components, so consumers read it as "the same shape as the enclosing schema `Name`".
- **`fragment_hash` is what the diff stage keys on.** It covers the resolved operation plus everything it inherits (referenced schemas, security requirements and the schemes they use, servers), so a change to a shared definition marks exactly the operations that depend on it.
- **`spec_hash`** hashes the whole parsed document, independent of key order and formatting. An operation without an `operationId` gets a synthesized one; duplicates are an error.
- **Docs are read strictly.** Markdown, text and diagrams-as-text are loaded in sorted order and bounded in count and size. Anything that cannot be read in full (unreadable, too large, not UTF-8) is an error rather than a silent omission.

## Execute and report

`hermetic run SUITE --target URL` runs a pytest suite, records the run and publishes reports. Later milestones put generation in front of it; this part does not change.

- **Pytest runs in-process, with a plugin.** The plugin gives tests an `hermetic_client` fixture (an httpx client bound to the target) and turns each pytest report into a `TestResult`. In-process keeps the plugin under the coverage gate and gives it the exceptions and assertion text directly. The cost is that a suite shares the interpreter, so modules the run imported from the suite are evicted afterwards (modules loaded before the run and installed packages are kept); otherwise two suites with a `test_api.py` would silently share one cached module.
- **Tests may only call the target.** The client refuses any request whose scheme, host and port differ from `--target` before it is sent. A test that builds its own client is outside this check, so it is a guardrail for generated tests, not a sandbox. Generated mocks will be added to the allowed origins when provisioning exists.
- **Recording is redacted at capture.** Headers whose names look like credentials (matched by fragment: `Authorization`, `Cookie`, `Api-Key`, `X-*-Token`, ...), JSON fields with secret-looking names, URL userinfo and secret-looking query parameters (including in `Location`, `Content-Location` and `Refresh`) are masked before anything is stored, so no later stage or sink can leak them. The same goes for `--target` in the run record. Credential values the test sent or received (sensitive headers and each cookie value, URL userinfo and query values, secret-looking JSON fields) are also masked, in recorded bodies as well,, if four or more characters long, wherever they appear in a failure message, because assertion introspection quotes compared values; masking happens before the message is cut to length. Not recognised: secrets in non-JSON bodies. Bodies are bounded, and a response body is captured only when it announces a small `Content-Length` and no `Content-Encoding`, because reading it inside the hook would make a streaming, endless or compressed response block or balloon the test.
- **The run itself can fail.** `--target` must be `http(s)://host[:port]`; error messages show it without credentials. A run that pytest interrupted (`pytest.exit`, Ctrl-C) is an error, never a partial pass; a collection error is the exception and is reported as an errored test.
- **A failure carries a repro.** For a failed or errored test the record holds a `curl` for the last request it made. Redacted headers become double-quoted `$NAME` placeholders so the command works once the variable is set. A body field that was redacted stays `[REDACTED]` in the command.
- **Status.** A failing assertion or any exception in the test body is `failed`. A setup or teardown error, or a module that cannot be imported, is `error`. A teardown error after a failed call keeps the assertion text and appends the teardown error; after a setup error the first message is kept. Skips and expected failures are `skipped`. A test marked `@pytest.mark.operation("id")` is tied to that `operationId`, which the diff stage will use.
- **Sinks.** A sink is a factory `(SinkContext) -> ReportSink` and a sink publishes a finished `RunRecord`. `markdown`, `html`, `json` and `stdout` are built in. Others register under the `hermetic.sinks` entry-point group; a built-in name cannot be replaced by a plugin. The HTML report is one file with no script and escapes everything recorded. Exit code is 0 when all tests pass, 1 when any fail or error, 2 when the run itself could not happen. A sink that fails to publish does not hide that: the run is already saved, the other sinks still run, the error is printed, and the exit code is still 1 if tests failed (2 otherwise).

## Run history

Flat files, no database. Each run is `.hermetic/runs/<run_id>.json` plus an `index.json` for fast listing. Access is through a `RunStore` protocol (`save`, `get`, `list`) with `FileRunStore` as the only v1 implementation, so a database store can replace it later without changing the CLI or UI. `FileRunStore` writes each file through a temp file and rename, validates run ids so they cannot name a path, and rebuilds `index.json` from the run files when it is missing or corrupt (skipping unreadable runs); a failed save drops the index so the next read rebuilds it rather than omitting the run. It does not lock: two processes saving at once can lose an index entry. The run file is intact, and deleting `index.json` rebuilds it. `.hermetic/runs/` is gitignored; the test manifest at `tests/.hermetic/manifest.json` is not history and is committed.

A run record holds metadata (target, spec/doc hashes, prompt and tool versions), per-test results (with expected vs actual, redacted request/response and a `curl` command for failures), and the suite changes since the previous run: tests added, modified and dropped, each with a reason.

`hermetic serve` runs a read-only web UI over the store: a history table with a pass-rate trend, and a run detail view with Failed / Added / Modified / Dropped / All tabs. The same data is exposed as JSON at `/api/runs`.

## Safety

- Tests only reach `--target` and the generated mocks, never real downstream hosts. API keys are never logged.
- Provisioning mounts the host Docker socket, which is root-equivalent on the host. Every agent-generated compose file is validated in deterministic code before it runs. Rejected: privileged containers, host network/PID/IPC, added capabilities, device mounts, bind mounts outside the workdir, the Docker socket itself, and images outside the approved list. A rootless Docker or Podman socket is the recommended hardened setup.

## Distribution

Releases are tag-driven and defined in `.github/workflows/release.yml`, which reuses `.github/workflows/ci.yml` as its gate. They publish a container image to GHCR and the package to PyPI via trusted publishing.
