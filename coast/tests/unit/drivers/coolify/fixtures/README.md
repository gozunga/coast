# Coolify HTTP fixtures

Live capture: **2026-10-10 (UTC)**. The recorded release is identified
by the fixture filename below; supported releases are defined in
`coast/common/engine_versions.py`.

`live.json` contains sanitized public API request/response pairs from a
successful contract lifecycle on two worker placements. Repeated deployment
polls retain the last response for each observed state. `v4.4.3.json` selects
response bodies from those pairs for offline driver tests. Both git and
Docker-image service calls are present in the paired capture. Resources created
by the suite are deleted; existing worker names and the team fallback flag
are restored before fixtures are written. Existing keys/workers are retained.

To re-record, follow `coast/tests/contract/coolify/README.rst` and set
`COAST_CONTRACT_COOLIFY_RECORD=1`. The destination is this directory.
Recording is watchdog-only. Use an isolated engine pinned to the selected
release from `coast/common/engine_versions.py`. Inspect the fixture diff
and run `tox -e py3 -- coast.tests.unit.drivers.coolify` before committing.
No controller address or credential is stored in these fixtures.

Headers are omitted. Tokens, keys, stack values, emails, infrastructure
references, non-documentation addresses and hostnames are replaced. Arbitrary
string fields and log content are redacted; log timestamps and serialization
shapes remain intact. Internal numeric IDs use stable numeric placeholders.
These fixtures test parsing and reconciliation, not the original log content.

`synthetic.json` contains explicit offline edge cases, not live captures:
project-list recovery, missing production-stack creation, SSH-key
creation/deletion, fresh worker/build registration, adopted-server
placement by recorded uuid, queued-deployment
handling, mixed persistent/file mount reconciliation, injected HTTP errors,
image-tag rollback, and deterministic secret-masking log examples. These paths remain synthetic
because the live suite preserves existing keys/workers and the engine
creates the production stack automatically. Queue polling can observe
in-progress immediately. Tests prefer live response bodies where available;
synthetic log cases are used only for deterministic masking/filter checks.

`requests.json` remains the pinned OpenAPI request-schema contract, not a
capture. Tests reject outgoing fields absent from the pinned public API
validation.
For application env POST/PATCH, `is_buildtime` and `is_runtime` are accepted
by the pinned public controller but omitted from OpenAPI. The legacy
`is_build_time` alias is rejected by its allowed-field check. Source:
the selected release's `app/Http/Controllers/Api/ApplicationsController.php`
in https://github.com/coollabsio/coolify (select the tag from
`coast/common/engine_versions.py`).

The OpenAPI requires both stack selectors, but their descriptions permit either; tests use
that documented alternative.

Sources:
the selected release's `openapi.json` in https://github.com/coollabsio/coolify
https://coolify.io/docs/api-reference

`compose-settings.json` is a synthetic pinned-public-API observation for the
c4rc2 compose domain regression. It is not a live recording. It contains
only domain target names, example.com URLs and normalized running state.

`hidden-build-log.json` is a synthetic deployment-log array, not a live
recording. It covers hidden docker-build output, secret masking, and a
blank hidden entry. The command text is not returned.

`deploy-key-rotation.json` contains synthetic public API response bodies for
in-place key updates and partial deletion retries. Request payloads are
validated against `requests.json`, including the documented private-key
PATCH. The fixtures contain no private material. Tests preserve the key UUID
across update and rollback and retain unrelated keys during deletion.

`pasted-sources.json` contains synthetic repository-free create and editor
responses. The Dockerfile create request schema is derived from the selected
release OpenAPI, as for the other entries in `requests.json`.

Dockerfile creation uses base64 content; the documented application update
`dockerfile` field is plain text. The synthetic editor assertion checks
that the create encoding is not reused on PATCH.
