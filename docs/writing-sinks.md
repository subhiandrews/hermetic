# Writing a sink

A **sink** is where a finished run goes: a file, the terminal, an issue tracker, a chat channel. Hermetic
ships four (`markdown`, `html`, `json`, `stdout`). Anyone can add another as a separate Python package;
Hermetic finds it through an entry point and runs it with `--report NAME`.

A sink only reads. By the time it is called the run has been executed and saved to the history store, so a
sink that fails never loses a run.

## The interface

```python
from hermetic.history.models import RunRecord
from hermetic.report import SinkContext  # also: ReportSink, SinkFactory


class MySink:
    def __init__(self, context: SinkContext) -> None: ...  # the factory: build it once per run
    def publish(self, record: RunRecord) -> None: ...  # deliver one finished run
```

- The **factory** is any callable `(SinkContext) -> ReportSink`. A class whose `__init__` takes the
  context is the usual one.
- `SinkContext` holds `output_dir`, the directory `--report-dir` names (default `hermetic-reports`). It
  grows only by adding fields, so a sink written today keeps working.
- `RunRecord` is a frozen Pydantic model: the service, run id, times, target, spec and document hashes,
  `results` (one `TestResult` per test) and `changes` (tests added, modified or dropped since the run
  before, each with a reason). A failed or errored `TestResult` carries its `message` and its `exchanges`,
  and a `curl` that reproduces the failing request when the test made one (`curl` is `None` otherwise). See `src/hermetic/history/models.py` for every field.

Secrets are masked before a record exists: headers, URL credentials and secret-looking body fields are
already redacted in `exchanges` and `curl`. A sink should still send only what its destination needs, and
treat every string in the record as untrusted text (a failing test can echo anything the service sent).

## An example

A sink that writes one line per failed test to `failures.txt`:

```python
# my_sink/__init__.py
from hermetic.history.models import RunRecord
from hermetic.report import SinkContext


class FailuresSink:
    def __init__(self, context: SinkContext) -> None:
        self._path = context.output_dir / "failures.txt"

    def publish(self, record: RunRecord) -> None:
        failed = [r for r in record.results if r.status in ("failed", "error")]
        lines = [f"{record.service} {record.run_id}: {len(failed)} failing"]
        lines += [f"{r.test_id}\n  {r.curl or '(no request made)'}" for r in failed]
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text("\n".join(lines) + "\n", encoding="utf-8")
```

Register it in the package's `pyproject.toml`, under the `hermetic.sinks` group. The name on the left is
what users pass to `--report`:

```toml
[project.entry-points."hermetic.sinks"]
failures = "my_sink:FailuresSink"
```

Install the package in the same environment as Hermetic, then:

```bash
hermetic run --target http://localhost:8080 --report stdout --report failures
```

`--report` is repeatable; sinks run in the order given.

## What Hermetic does for you

- **Names.** A built-in name cannot be replaced by a plugin, so installing a package never changes what
  an existing command does. An unknown name is an error that lists the names available.
- **Loading.** The entry point is imported and the factory called only when a run asks for that sink.
  If either step raises, the run stops before any test runs, with a message naming the sink.
- **Failing.** If `publish` raises, the other sinks still run, the error is printed, and the exit code is
  what the tests decided: 1 if any failed, otherwise 2 (the sink's failure). The run is already in the
  history either way.
- **Exit codes** are not the sink's to set (see Execute and report in `docs/architecture.md`).

## Testing a sink

Build a `RunRecord` by hand and call `publish`; no Hermetic process is needed (Hermetic's own sink tests
in `tests/unit/test_report.py` do this). To test the registration, install the package and run
`hermetic run --report <name>`.

## Limits to know

- `publish` is synchronous and has no timeout of its own: a sink that calls a network service should set
  one, or a slow service delays the exit of `hermetic run`.
- A sink receives the whole record, which can be large for a big suite. Write or send what you need, not a
  copy of everything.
- Issue-tracker sinks (Linear, Jira) are not part of Hermetic; they are meant to be packages like this one.
