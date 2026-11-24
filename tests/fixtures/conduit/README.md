# Conduit fixture

`openapi.yml` and `LICENSE` come unmodified from the RealWorld ("Conduit") project,
<https://github.com/gothinkster/realworld>, file `specs/api/openapi.yml` at commit
`4248dbe042138a357578c15a655a38c12abba353`, downloaded on 2026-11-24. The project is published under
the MIT License (copy in `LICENSE`). It is used only as test input; no test calls a live service.

The rest is written for Hermetic's tests:

- `docs/architecture.md` is a design note for a Conduit backend that names its dependencies,
  the way a team's own documents would.
- `mail-openapi.yml` is the OpenAPI file of the one HTTP service that note says Conduit calls.
- `expected/` holds the files the pipeline must produce from `openapi.yml`, `docs/` and `mail-openapi.yml`. Change them
  only on purpose: run the tests with `HERMETIC_UPDATE_GOLDEN=1` to rewrite them, then read the diff.
