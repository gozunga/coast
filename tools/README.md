# Tools

Developer and operator helper scripts:

- [build-images/](build-images/README.rst): diskimage-builder elements,
  guest agent archive builder, and offline controller image inputs.
- `gen-ca.sh`: creates separate Coast server/client CAs and a controller
  client certificate. See [operations](../doc/source/operations.md).
- [coolify/](coolify/README.rst): isolated bootstrap prototype and contract
  setup instructions; not a shared-cloud deployment procedure.
- [kolla/](kolla/README.rst): Kolla-Ansible role and image overlay.
- [walkthrough/](walkthrough/README.md): tenant CLI walkthrough and cleanup.

## Build-time one-click template catalog (Q95)

`gen-template-catalog.py` uses `coast.common.named_volumes` (which uses
the Coast Compose parser and PyYAML) and `coast.common.engine_names`. Run it
where Coast's requirements are installed, for example `tox -e
template-catalog`. It reads a local Coolify `templates/service-templates.json`
from the tag named by `TEMPLATE_VERSION` in `coast/common/engine_versions.py`.
Local-source mode makes no network calls; `--check-pinned` (what
`tox -e template-catalog` runs) downloads the digest-locked source.
`tools/template-catalog-source.json` locks the source URL and its SHA256.

Fetch and check the source before use, as with `tools/build-images`.
Compute and record the expected SHA256 on the build host from the reviewed
pinned source. Do not copy a checksum from this tree. Replace `<sha256>`
below with that recorded digest. A missing checksum or mismatch stops the
generator before it writes a catalog.

```bash
python3 -c 'from coast.common.engine_versions import TEMPLATE_VERSION; print(TEMPLATE_VERSION)'
# Set TV to the printed value.
curl -fsSL \
  "https://raw.githubusercontent.com/coollabsio/coolify/v$TV/templates/service-templates.json" \
  -o /tmp/service-templates.json
sha256sum /tmp/service-templates.json
python3 tools/gen-template-catalog.py /tmp/service-templates.json \
  --version "$TV" --sha256 <sha256>
```

The default output is
`coast/drivers/coolify/templates/catalog-$TV.json`,
`volumes-$TV.json` and `compose-$TV.json` (the pinned base64 compose
per template, used for creates) in the same directory. Use `--output-dir`
to select a staging directory. The version has no `v` prefix and must
match the source tag and the target engine version. The checksum checks
file integrity, not the tag identity; the release operator must check both.

Output is a JSON list sorted by template id, with sorted unique tags,
environment keys, and integer ports. Each entry contains only `id`, `name`,
`description` (the source slogan), `logo`, `category`, `tags`, `required_env`,
`ports` (the source comma-separated `port` field), and `http_port`. If the source has no
name, the template key is used. Missing metadata has empty values.
Project users read `name`, `description`, `category`, `tags` and `logo`, so
the generator replaces the app engine and proxy names in them with neutral
wording and replaces em dashes with hyphens. A `logo` file named after
either, or with an em dash in its name, is dropped (it becomes empty).
Template ids and `required_env` names are identifiers and stay as upstream
has them.

`required_env` contains variable names referenced by the decoded compose
and keys from optional `envs` maps or `KEY=value` lists. It does not infer
which variables are mandatory or parse arbitrary YAML. The metadata catalog contains no Compose content or environment values.
The separate `compose-$TV.json` asset retains the upstream base64 Compose,
including interpolation defaults, for reproducible template creation. `SERVICE_*` key names can be listed; Coolify
creates their values. Invalid source data, including non-numeric ports, stops generation without
writing output. Numeric ports outside 1–65535 are omitted.
Generation does not validate upstream workloads for the operator. Generation does not prove deployability.

The volume file records named-volume capacity accounts (`name` and
`container_path`), with `docker_socket` when detected. It is not a safety
verdict. Bind, anonymous and read-only mounts do not consume named-storage
accounts; they do not hide or reject a template. `--check` compares all
three generated assets without writing them. The driver and template API
consume these packaged assets.

## Guest control-plane isolation check (M30)

Run `check-control-plane-isolation.py` as root on a worker, build server,
or engine VM. The script binds to the environment NIC. TCP/9600 and
TCP/5672 to `--control-plane-host` must time out. Public HTTPS on that NIC
must succeed. A connection refusal is not a pass. See networking.md.

```console
sudo python3 tools/check-control-plane-isolation.py --interface ENV_NIC \
  --control-plane-host CONTROL_PLANE_IP --public-host example.com
```

## Fast lanes (CI and local)

Merge-request pipelines only run the jobs whose inputs changed
(`rules: changes:` in `.gitlab-ci.yml`); `pep8` always runs. Pipelines on
`main`, scheduled (nightly) and web-triggered pipelines run every job, so
nothing that a narrowed MR pipeline skipped can reach a release unnoticed.
Changing `setup.cfg`, requirements, `tox.ini`, `.stestr.conf` or
`.gitlab-ci.yml` runs everything on the MR too.

* `ci-shard.sh N TOTAL` runs one slice of the unit suite; the py3 job
  runs it whole in one job. Shards are balanced on `ci-shard-times.json`
  (per-class CPU seconds). Refresh it after large test changes with
  `tox -e py3 && tools/ci-shard.sh --update-times`. Stale timings only
  unbalance the shards; every test still runs exactly once.
* `fast-check.sh [base-ref]` is the local pre-merge check for hot-patch
  lanes: flake8 on changed Python files, then only the unit tests that are
  named after or import a changed module (whole suite when build files
  change). It does not replace the MR pipeline.
