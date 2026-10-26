# coast/tests

- `unit/`: fast, no network, sqlite in-memory, fake RPC, `noop` driver,
  requests-mock for HTTP. Run: `tox -e py3`.
- `functional/`: API-through-conductor flows with real DB + fake clouds.
  Run: `tox -e functional`.
- `contract/coolify/`: manual contract tests against a live Coolify on a
  supported release (`tox -e contract-coolify`).
Base classes: `coast/tests/base.py` (oslotest).
