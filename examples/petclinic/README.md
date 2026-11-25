# PetClinic example

A service with one database and nothing else: the REST backend of Spring PetClinic, 37 operations.

`openapi.yml` and `LICENSE` come unmodified from
[spring-petclinic-rest](https://github.com/spring-petclinic/spring-petclinic-rest), file
`src/main/resources/openapi.yml` at commit `220f4ba8e92cec9ad1faa03324723a3e9da63d95`, downloaded on
2026-11-24. The project is published under the Apache License 2.0 (copy in `LICENSE`).

The rest is written for Hermetic:

- `docs/architecture.md` is a design note that names the service's one dependency (PostgreSQL), the way
  a team's own documents would. It also says the service calls no other HTTP service.
- `expected/` holds the files Hermetic must produce from the two inputs above. They are checked by
  `tests/unit/test_petclinic.py`; change them only on purpose, by running the tests with
  `HERMETIC_UPDATE_GOLDEN=1` and reading the diff.

## Try it

The commands write `tests/` and `.hermetic/` into the current directory, so copy the example out of the
repository first:

```bash
cp -r examples/petclinic /tmp/petclinic && cd /tmp/petclinic
hermetic discover openapi.yml --docs docs --service petclinic   # finds PostgreSQL
hermetic compose --service petclinic                            # writes the mock stack
hermetic up --service petclinic                                 # starts it; needs Docker
hermetic down --service petclinic
```

`discover` reads the design note with the agent, so it needs credentials for the Claude API. The service
under test is not part of the stack: run PetClinic yourself (the spec's own `servers` entry puts it at
`http://localhost:9966/petclinic/api`) and point `hermetic run --target` at it.

The upstream spec uses `\p{L}` in its patterns. That is valid in the ECMA 262 syntax OpenAPI uses but not
in Python's `re`; Hermetic accepts it and keeps the text as written.
