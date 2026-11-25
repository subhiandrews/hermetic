# Sock Shop example

A service that has a database and calls three other HTTP services: the orders service of the
[Sock Shop](https://github.com/microservices-demo/microservices-demo), a demo online shop. All four
APIs are described by **Swagger 2.0** files, so this is also the example for Hermetic's Swagger 2.0 input
(it converts each file to OpenAPI 3.0.3 as it reads it).

| File | What it is |
| --- | --- |
| `specs/orders.json` | The service under test (2 operations). |
| `specs/carts.json`, `specs/payment.json`, `specs/user.json` | The services it calls, mocked from their specs (5, 2 and 15 operations). |

The four specs and `LICENSE` come unmodified from the `microservices-demo` repositories, downloaded on
2026-11-24: `orders` at `546a10c92d1ff9998c21f3849f5b95a0f14430a0` (`api-spec/orders.json`), `carts` at
`f4e80051668c6a83359c41b03a27baefa590adc8` (`api-spec/carts.json`), `payment` at
`384e33447dda5334187517dda09566c9a17fd6af` (`api-spec/payment.json`) and `user` at
`e1a79e77860734cacc3f64cabff1487e22a442a4` (`apispec/user.json`). The projects are published under the
Apache License 2.0 (copy in `LICENSE`; the four copies are identical). The orders spec says "MIT" in
its `info.license`, which is a leftover from the template it was written from.

The rest is written for Hermetic:

- `docs/architecture.md` is a design note for the orders service. It names its database (MongoDB, by
  connection string) and the three services it calls.
- `expected/` holds the files Hermetic must produce from the inputs above: `dependencies.json`, the
  compose file (MongoDB plus one WireMock container per service) and each service's stubs. They are
  checked by `tests/unit/test_sockshop.py`; change them only on purpose, by running the tests with
  `HERMETIC_UPDATE_GOLDEN=1` and reading the diff.

Left out: the real shop's orders service also calls the shipping service, which publishes no API
description, so there is nothing to mock it from. Its catalogue and front-end services are not
dependencies of orders.

## Try it

The commands write `tests/` and `.hermetic/` into the current directory, so copy the example out of the
repository first:

```bash
cp -r examples/sockshop /tmp/sockshop && cd /tmp/sockshop
hermetic discover specs/orders.json --docs docs --service orders   # finds MongoDB, carts, payment, user
for s in carts payment user; do
  hermetic stubs --service orders --name $s --spec specs/$s.json   # writes each service's stubs
done
hermetic compose --service orders                                  # writes the mock stack
hermetic up --service orders                                       # starts it; needs Docker
hermetic down --service orders
```

`discover` reads the design note with the agent, so it needs credentials for the Claude API. The service
under test is not part of the stack: run the orders service yourself and point `hermetic run --target`
at it.
