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
- **Secrets are masked at capture.** Sensitive headers, URL credentials, and secret-looking fields in JSON and form bodies are masked as a request is recorded, and any credential a test sent or received is also masked in its failure text and in the exchanges it recorded. A body too deeply nested to redact is omitted. Test ids are masked against the credentials the test saw, but pytest uses parametrize values verbatim in ids, so a credential used as a parametrize value should not also be the id. Known gaps: secrets in other text bodies are not recognised, and matching is by name fragment, so some fields (`tokens_used`) are over-masked, which is the side to err on.
- **Response bodies are captured only when cheap and safe.** The client asks for uncompressed bodies, and the hook records one only when the response announces a small `Content-Length` and no `Content-Encoding`. Reading it unconditionally would make a streaming or endless response block the test, and decoding a compressed one is unbounded. Credentials in an unrecorded body are not collected.
- **The run itself can fail.** `--target` must be `http(s)://host[:port]`; error messages show it without credentials. A run that pytest interrupted (`pytest.exit`, Ctrl-C) is an error, never a partial pass; a collection error is the exception and is reported as an errored test.
- **A failure carries a repro.** For a failed or errored test the record holds a `curl` for the last request it made. If that request's body was binary or cut short, the curl omits the body and says so rather than sending something different. Redacted headers become double-quoted `$NAME` placeholders so the command works once the variable is set. A body field that was redacted stays `[REDACTED]` in the command.
- **Status.** A failing assertion or any exception in the test body is `failed`. A setup or teardown error, or a module that cannot be imported, is `error`. A teardown error after a failed call keeps the assertion text and appends the teardown error; after a setup error the first message is kept. Skips and expected failures are `skipped`. A test marked `@pytest.mark.operation("id")` is tied to that `operationId`, which the diff stage will use.
- **Sinks.** A sink is a factory `(SinkContext) -> ReportSink` and a sink publishes a finished `RunRecord`. `markdown`, `html`, `json` and `stdout` are built in. Others register under the `hermetic.sinks` entry-point group; a built-in name cannot be replaced by a plugin. The HTML report is one file with no script and escapes everything recorded. Exit code is 0 when all tests pass, 1 when any fail or error, 2 when the run itself could not happen. A sink that fails to publish does not hide that: the run is already saved, the other sinks still run, the error is printed, and the exit code is still 1 if tests failed (2 otherwise).

## Generate and diff

`hermetic generate SPEC` (and `hermetic run --generate`) writes one pytest module per operation, into `tests/<service>/` unless `--suite DIR` says otherwise, and rewrites only what changed. The agent sits behind the `SuiteAuthor` protocol (`write_test(request) -> source + note`); unit tests use a fake, and `ClaudeAuthor` is the Agent SDK implementation.

- **The manifest** is `<suite>/.hermetic/manifest.json` and is committed. Per operation it records the test file, the operation's `fragment_hash`, the fragment itself (for before/after), the prompt version and why the test was last written. A test file name read back from it must equal the name computed from the operation id (`test_<slug>_<hash>.py`), or the manifest is rejected, since the file is editable and a name is used to write and delete files.
- **What triggers a rewrite:** the fragment hash changed (including through a shared schema), the prompt version changed, its test file is missing or is a symlink, or `--force`. A new operation is added; one missing from the spec is dropped and its file deleted. A docs edit alone does not rewrite tests, because docs are not tied to one operation; use `--force`.
- **Reasons come from the diff, not the agent.** The diff supplies the cause and the agent's one-line note is appended. The run record stores them as the added/modified/dropped changes; a change's `test_id` is the generated file name.
- **Authoring is all or nothing; writing is not.** Tests are authored up to four at a time, and every one is validated before any file is written, so a rejected or failed test leaves the suite and manifest as they were. If writing fails partway (a full disk), some files can be ahead of the manifest, and the next run rewrites them.
- **Generated source is validated before it is written** (`generate/validate.py` holds the full rule list): it must parse, import only an allowlist (not `httpx`, so every request goes through the origin-checked `hermetic_client`), avoid file-system and environment fixtures, dynamic builtins, private and dunder access and attribute assignment, and mark every `test_` function with `@pytest.mark.operation("<its operationId>")`. Because the suite runs in-process with the user's privileges, this is a guardrail against a confused agent, not a sandbox.
- **The agent gets no tools.** It sees the operation, the docs (truncated) and the previous test inside delimiters it is told to treat as data, and returns text. Closing tags in that data are broken up so they cannot end the block, and spec-derived one-liners (title, servers, operationId, doc paths) are sent as JSON string literals. `PROMPT_VERSION` in `agent/claude.py` is bumped whenever the prompt changes in a way that should rewrite every test.

## Run history

Flat files, no database. History is grouped by **service**, the microservice a run tested: each run is `.hermetic/runs/<service>/<run_id>.json` plus an `index.json` per service for fast listing, so every service has its own history. Access is through a `RunStore` protocol (`save`, `get(service, run_id)`, `list(service=None)`, `services`) with `FileRunStore` as the only v1 implementation, so a database store can replace it later without changing the CLI or UI. A run id is unique within its service. `FileRunStore` writes files atomically, validates run ids and service ids so they cannot name a path, and reconciles each `index.json` with its run files on every read: a missing, corrupt or stale index (two processes saving at once, a crash between the two writes) is repaired from the run files. It takes no lock, so concurrent saves can briefly drop an index entry until the next read. History written before services existed (`.hermetic/runs/<run_id>.json`) is moved under the `default` service the first time a process uses the store; other files in the directory are left alone.

A **service id** is lowercase letters, digits and hyphens (at most 63, starting with a letter or digit), because it names a directory and later a URL segment. `--service` sets it; without it the id is a slug of the OpenAPI title, so `hermetic run` without `--spec` needs `--service`. A rename of the spec title would start a new history, which is why `--service` is the stable way to name one.

A run record holds metadata (target, spec/doc hashes, prompt and tool versions), per-test results (with expected vs actual, redacted request/response and a `curl` command for failures), and the suite changes since the previous run: tests added, modified and dropped, each with a reason.

`hermetic serve` runs a read-only web UI over the store: a history table with a pass-rate trend, and a run detail view with Failed / Added / Modified / Dropped / All tabs. The same data is exposed as JSON: `/api/runs` lists the runs of every service (each carries its `service`), and `/api/services/<service>/runs/<run_id>` returns one, since a run id is only unique within its service. It is a small FastAPI app (`web/app.py`) that depends only on the `RunStore` protocol, plus a static page (`web/static/`) with no build step.

- **Run records are untrusted data.** They hold request and response bodies from the service under test, so the page only ever inserts them as text, never as markup (a test fails if the script uses `innerHTML` and the like). The content security policy forbids inline script and style, and every response carries it.
- **It listens on loopback by default** and refuses a `Host` header it was not started for, so a web page cannot reach it through DNS rebinding (the page's own domain, pointed at the server, is not on the list). Listening beyond loopback prints a warning, because anyone who can reach the port can read the history, but keeps the check. `--allowed-host` extends the list and can switch the check off, with a warning; `hermetic serve --help` has the details.
- **Store errors never reach a client or the log.** A missing or malformed run id is a 404 and an unreadable run is a generic 500; every response, errors included, carries the security headers. The store's message can quote the record (a corrupt file's validation error does), so only the kind of failure is logged.
- **The run list is capped**, because history only grows; the page says when it shows a partial list.

### Central history

`hermetic serve` can also be the place runs are kept. The API is the interface: the CLI and the page only use it, so the file store behind it can later be a database. `GET /api/services` lists the services, `GET /api/services/<service>/runs` and `/<run_id>` read them, and `POST /api/services/<service>/runs` records one. `hermetic run --history-dir https://host` does that through `HttpRunStore`, which passes the same contract tests as the file store.

- **Ingest is off unless the server has `HERMETIC_API_TOKEN`, and the token must be at least 32 characters** (`serve` refuses a shorter one), because it is the only guard on writes and is not rate limited. It is a bearer token, compared in constant time and checked before the body is read. With none configured, a POST is refused, so a local process or a web page cannot write history. The client reads the same variable.
- **The token only travels where it is safe.** `HttpRunStore` refuses to send it over plain `http` to a host that is not on this machine, does not follow redirects, and never puts the token, the address's credentials or a response body in an error. A server reachable beyond loopback should sit behind TLS; `serve` says so.
- **The body is untrusted.** It is capped at 10 MB, parsed as a run record, and must name the service in the address and a run id the store accepts. A rejected body is answered with where and what was wrong, never the value; a client-chosen dict key is shown as `*`. A run with an existing id replaces it, as in the store protocol.
- **The server must allow the client's host name** (`--allowed-host`), as for any other client. Reads are not authenticated yet; that is needed before a server is shared beyond one team.

## Safety

- Tests only reach `--target` and the generated mocks, never real downstream hosts. API keys are never logged.
- Provisioning mounts the host Docker socket, which is root-equivalent on the host. Every agent-generated compose file is validated in deterministic code before it runs. Rejected: privileged containers, host network/PID/IPC, added capabilities, device mounts, bind mounts outside the workdir, the Docker socket itself, and images outside the approved list. A rootless Docker or Podman socket is the recommended hardened setup.

## Distribution

Releases are tag-driven and defined in `.github/workflows/release.yml`, which reuses `.github/workflows/ci.yml` as its gate. They publish a container image to GHCR and the package to PyPI via trusted publishing.
