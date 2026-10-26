# coast/api - REST API process (`coast-api`)

The API uses pecan and WSME behind keystonemiddleware. It validates input,
enforces policy and writes desired state through project-scoped objects.
It never calls Coolify or guests directly. Conductor handles async work.

The Q94 contract is stack > appgroup > environment > service:

- `/v1/stacks`: infrastructure, including nested servers, volumes and exports.
- `/v1/stacks/{stack_id}/appgroups`: appgroup list and create.
- `/v1/appgroups/{appgroup_id}`: appgroup show, update and delete.
- `/v1/appgroups/{appgroup_id}/environments`: environment list and create.
- `/v1/environments/{environment_id}`: environment show, update and delete.
- `/v1/services`: flat service list and create. Create requires both
  `appgroup_id` and `environment_id`; the environment must belong to
  that appgroup. A supplied worker must belong to the same stack.
- `/v1/services/{service_id}`: service show, update and delete, with nested
  actions, deployments, domains and environment variables.

`GET /` and `GET /v1` are public version discovery. Resource routes require
authentication. The catalog type remains `app-hosting`. Clients send
`OpenStack-API-Version: app-hosting <version>`; the supported range is in
`coast/api/microversions.py`. Other top-level routes (`/v1/apps`,
`/v1/events`, `/v1/backups`, `/v1/plans`, `/v1/flavors`, `/v1/templates`,
`/v1/quotas`, `/v1/dns`) are mounted in `root_controller.py`.
See [api.md](../../doc/source/api.md) for the full contract.
Q95 template reads use packaged metadata. Template create stores
`template_id` and returns 202 after the catalog, policy and allow/deny
checks.
Q47 event strings retain `environment` for stacks and `app` for services.
Quota keys and configuration option names remain unchanged, including
`environments` and `apps`.
