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

`hermetic.ingest` turns an OpenAPI 3.x file (JSON or YAML) and an optional docs directory into a frozen `ServiceModel`. Decisions that later stages depend on:

- Only OpenAPI 3.x is accepted; the document is validated first and a failure stops the run.
- The spec is untrusted input. YAML is normalised to plain JSON types on load (integer keys such as `200:` become strings, dates become ISO strings, other non-JSON values are rejected, and keys YAML 1.1 reads as booleans or null, such as an unquoted `on`, `yes` or `null`, are rejected with a hint to quote them rather than silently renamed), and node caps apply to the loaded document, to each path item's inlined expansion and to the total expansion across the document, so an alias bomb or schemas that fan out into each other fail with a clear error instead of exhausting memory.
- Only local `$ref` pointers (`#/...`) are resolved. External references are rejected before the validator runs, because the validator's resolver would otherwise fetch URLs or read files. A reference to a schema that is already being expanded (a recursive schema) is left as `{"$ref": ...}` rather than expanded forever.
- Each operation carries a `fragment_hash`: a hash of the fully resolved operation, including any component schema it references, the security requirements and servers it inherits (operation, then path item, then document), and the definitions of the security schemes it uses (a change to an unrelated scheme does not mark it). The diff stage uses it to find added, modified and dropped operations, so a change to a shared schema correctly marks every operation that uses it.
- A parameter described with `content` instead of `schema` keeps its schema, and its media type is recorded in `media_type`.
- `spec_hash` hashes the whole parsed document, independent of key order and formatting.
- An operation without an `operationId` gets a synthesized one (`get_pets_id`); duplicate ids are an error.
- The docs loader reads Markdown, plain text and diagrams-as-text (PlantUML, Mermaid), sorted by path. It never enters hidden directories (so pointing it at a repository root does not walk `.git` or `.venv`) and skips hidden files, symlinks and special files, and fails on a file or directory it cannot read, a file that is too large, or one that is not UTF-8, instead of silently dropping it.

## Run history

Flat files, no database. Each run is `.hermetic/runs/<run_id>.json` plus an `index.json` for fast listing. Access is through a `RunStore` protocol (`save`, `get`, `list`) with `FileRunStore` as the only v1 implementation, so a database store can replace it later without changing the CLI or UI. `.hermetic/runs/` is gitignored; the test manifest at `tests/.hermetic/manifest.json` is not history and is committed.

A run record holds metadata (target, spec/doc hashes, prompt and tool versions), per-test results (with expected vs actual, redacted request/response and a `curl` command for failures), and the suite changes since the previous run: tests added, modified and dropped, each with a reason.

`hermetic serve` runs a read-only web UI over the store: a history table with a pass-rate trend, and a run detail view with Failed / Added / Modified / Dropped / All tabs. The same data is exposed as JSON at `/api/runs`.

## Safety

- Tests only reach `--target` and the generated mocks, never real downstream hosts. API keys are never logged.
- Provisioning mounts the host Docker socket, which is root-equivalent on the host. Every agent-generated compose file is validated in deterministic code before it runs. Rejected: privileged containers, host network/PID/IPC, added capabilities, device mounts, bind mounts outside the workdir, the Docker socket itself, and images outside the approved list. A rootless Docker or Podman socket is the recommended hardened setup.

## Distribution

Releases are tag-driven and defined in `.github/workflows/release.yml`, which reuses `.github/workflows/ci.yml` as its gate. They publish a container image to GHCR and the package to PyPI via trusted publishing.
