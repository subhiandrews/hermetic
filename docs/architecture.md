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

- **The spec is untrusted input.** It is validated first and parsed into plain JSON types: ambiguous YAML (an unquoted `on` key), duplicate keys and non-JSON values are errors. File size, node counts and the expansion of references are bounded, so a hostile or pathological file fails with a clear error instead of exhausting memory.
- **Only local `$ref`s (`#/...`) are resolved.** External ones are rejected before the validator runs, because its resolver would otherwise fetch URLs or read files. In 3.1 documents, keywords next to a `$ref` are merged over its target; 3.0 ignores them, as that version specifies. Known limitation: any object with a string `$ref` key is read as a reference, including literal data inside an `example` or `default`; telling schema positions from data positions needs a position-aware walk, which is not done yet.
- **Recursive schemas keep a reference.** A `$ref` back into a schema that is already being expanded is left as `{"$ref": "#/components/schemas/Name"}`. The model carries no components, so consumers read it as "the same shape as the enclosing schema `Name`".
- **`fragment_hash` is what the diff stage keys on.** It covers the resolved operation plus everything it inherits (referenced schemas, security requirements and the schemes they use, servers), so a change to a shared definition marks exactly the operations that depend on it.
- **`spec_hash`** hashes the whole parsed document, independent of key order and formatting. An operation without an `operationId` gets a synthesized one; duplicates are an error.
- **Docs are read strictly.** Markdown, text and diagrams-as-text are loaded in sorted order and bounded in count and size. Anything that cannot be read in full (unreadable, too large, not UTF-8) is an error rather than a silent omission.

## Run history

Flat files, no database. Each run is `.hermetic/runs/<run_id>.json` plus an `index.json` for fast listing. Access is through a `RunStore` protocol (`save`, `get`, `list`) with `FileRunStore` as the only v1 implementation, so a database store can replace it later without changing the CLI or UI. `.hermetic/runs/` is gitignored; the test manifest at `tests/.hermetic/manifest.json` is not history and is committed.

A run record holds metadata (target, spec/doc hashes, prompt and tool versions), per-test results (with expected vs actual, redacted request/response and a `curl` command for failures), and the suite changes since the previous run: tests added, modified and dropped, each with a reason.

`hermetic serve` runs a read-only web UI over the store: a history table with a pass-rate trend, and a run detail view with Failed / Added / Modified / Dropped / All tabs. The same data is exposed as JSON at `/api/runs`.

## Safety

- Tests only reach `--target` and the generated mocks, never real downstream hosts. API keys are never logged.
- Provisioning mounts the host Docker socket, which is root-equivalent on the host. Every agent-generated compose file is validated in deterministic code before it runs. Rejected: privileged containers, host network/PID/IPC, added capabilities, device mounts, bind mounts outside the workdir, the Docker socket itself, and images outside the approved list. A rootless Docker or Podman socket is the recommended hardened setup.

## Distribution

Releases are tag-driven and defined in `.github/workflows/release.yml`, which reuses `.github/workflows/ci.yml` as its gate. They publish a container image to GHCR and the package to PyPI via trusted publishing.
