# coast/drivers/coolify - Coolify driver

- `client.py`: thin HTTP client for the **documented** Coolify v4 REST API
  (`/api/v1/...`, bearer token from Barbican). No scraping, no DB access,
  no patches to Coolify.
- `v4.py`: `CoolifyV4Driver(CoastDriver)` - entry point `coolify_v4`.
- `catalog.py` and `templates/`: packaged one-click template metadata.
- `settings.py`: drift comparison for env vars, domains and compose
  targets.
- Mapping: appgroup -> Coolify project, environment -> Coolify environment,
  server -> Coolify server, app -> Coolify application, service -> Coolify
  service (which may contain databases), standalone database -> Coolify
  database. Database engine versions and images come from `DATABASES` in
  `coast/common/engine_versions.py`.
- Contract tests: `coast/tests/contract/coolify/` (`tox -e contract-coolify`).

Supported Coolify versions are listed in `SUPPORTED_ENGINE_VERSIONS` in `coast/common/engine_versions.py`.
