# Policy notes

Policies are **defined in code** (`coast/policies/*.py`, oslo.policy
`DocumentedRuleDefault`) and exposed via the `oslo.policy.policies` entry
point. Operators only override in `policy.yaml`; generate a sample with
`tox -e genpolicy`.

Defaults follow the OpenStack secure-RBAC personas (scope = project):

| Persona          | check string                                   |
|------------------|------------------------------------------------|
| project reader   | `role:reader and project_id:%(project_id)s`    |
| project member   | `role:member and project_id:%(project_id)s`   |
| project manager  | `role:manager and project_id:%(project_id)s`  |
| admin            | `role:admin`                                   |
| service          | `role:service`                                 |
| agent credential | `role:coast_agent`                             |

`AGENT` is `role:coast_agent`. No Coast API rule uses that check. The
Keystone user `coast-agent` has this role only. It is not `admin` and
not `service`. `coast:plan:get_all` still allows every project token
because its check is `@`.

Rule names: `coast:<resource>:<action>`, e.g. `coast:app:create`,
`coast:environment:delete`, `coast:server:get_detail` (admin-only fields),
`coast:quota:update` (admin). Full table: `doc/source/api.md#policies`.

`enforce_scope = true` and `enforce_new_defaults = true` from day one.
No legacy rules.
