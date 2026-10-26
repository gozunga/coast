# App sources

Deploy an app from a private Git repository or a Dockerfile, or create a
service from a Compose file. Start with an existing stack, app group,
environment and worker. The examples use `website`, `staging` and
`worker-1`; replace them with your resource names or UUIDs. See the
[client guide](client.md#worked-examples) to create these resources.

Private Git deploy keys need API 1.30. Repository-free Dockerfile and
Compose sources need API 1.31. The client requests these versions when
the corresponding source options are used; an older server reports
`App source needs API version 1.30 or later.` (or `1.31`). See the
[CLI source reference](client.md#app-source-commands).

## Deploy from a private Git repository

Use an SSH repository URL such as `git@git.example.com:team/web.git` or
`ssh://git@git.example.com:2222/team/web.git`. SSH sources require a deploy
key. HTTPS sources cannot use one, and URLs with embedded credentials
are rejected. Coast does not use GitHub Apps or GitLab Apps for this flow.

### Generate a key and deploy

Coast can generate an ed25519 key on the server. With the CLI, select
generation explicitly:

```console
$ openstack coast app create web --appgroup website --environment staging \
    --server worker-1 --repo-url git@git.example.com:team/web.git \
    --branch main --deploy-key generate --ports 8080
$ openstack coast app deploy-key show web
```

Creation shows the public key and leaves the app undeployed. Copy that
public key into the repository's deploy-key settings on GitHub, GitLab
or Gitea. Give it **read-only** access: leave write access disabled on
GitHub and Gitea, and grant only read access on GitLab. Add the public
key, never a private key. After the repository accepts the key, deploy:

```console
$ openstack coast deploy web
$ openstack coast deployment list --service web
$ openstack coast logs --service web --tail 50
```

In Horizon, choose the private Git source and generated-key option,
create the app, add the displayed public key to your repository, then
use Deploy. The app details provide its public key and fingerprint.
Coast stores the private key in Barbican and never returns it in API, CLI
or dashboard output. App or stack teardown deletes the owned key secrets.
Keep a separate deploy key for each
app so you can revoke access independently.

### Upload a key

You can upload an existing unencrypted OpenSSH or PEM private key instead.
Accepted key types are ed25519, ECDSA and RSA (at least 2048 bits).
Passphrase-protected keys are rejected with HTTP 400; choose an
unencrypted deploy key dedicated to this repository. Key size is also
subject to the operator's secret byte limit.

```console
$ openstack coast app create web --appgroup website --environment staging \
    --server worker-1 --repo-url git@git.example.com:team/web.git \
    --deploy-key-file /path/to/deploy-key --ports 8080 --no-deploy
$ openstack coast app deploy-key show web
```

Add the matching public key as a read-only repository deploy key, then run
`openstack coast deploy web`. Uploaded keys otherwise use the normal
create-and-deploy behavior; `--no-deploy` gives you time to configure access.
The dashboard also accepts an unencrypted private key upload.

### Rotate a key

Generate a replacement without deploying:

```console
$ openstack coast app set web --deploy-key generate
$ openstack coast app deploy-key show web
```

Add the new public key to the repository with read-only access, deploy
and check the deployment succeeds, then remove the old repository key.
To upload a replacement instead, use
`openstack coast app set web --deploy-key-file /path/to/new-deploy-key`.
Coast replaces the stored key and deletes its old secret after the server
has accepted the replacement. Rotation itself does not deploy.

## Deploy from a Dockerfile

Provide a self-contained Dockerfile as UTF-8 text; no repository is
required. In Horizon, choose Dockerfile and paste text or upload a file.
With the CLI:

```console
$ openstack coast app create web --appgroup website --environment staging \
    --server worker-1 --dockerfile ./Dockerfile --ports 8080 --no-deploy
$ openstack coast dockerfile show web
$ openstack coast deploy web
```

When the stack has a build server, Dockerfile apps build there and the
worker pulls the built image from the stack registry. Without a build
server, Dockerfile apps build on their worker.

There is no local build-context upload. `COPY` and `ADD` cannot refer to
files beside your local Dockerfile. Use a Git source when your build needs
repository files. Choose the container port your app actually listens on;
`--ports 8080` does not change its listening port. Ports are required for
HTTP health checks and domain routing, but optional for a background app.

The Dockerfile is fixed when the app is created, including when you use
`--no-deploy`. `app set APP --dockerfile FILE`, the dashboard's Dockerfile
editor and `PUT /v1/services/{id}/dockerfile` still save text without
deploying. On redeploy, unchanged Dockerfile text is skipped and the image
is reused; deploying changed text fails
before deployment starts with this message:

> This app can't use a changed Dockerfile. Create a new app from the new
> Dockerfile, or deploy from a Git repository.

To change the Dockerfile, create a new app from the new Dockerfile, or use
a Git repository source, whose Dockerfile changes deploy normally.

The text is stored server-side and omitted from ordinary create, list and show
output; `dockerfile show` explicitly retrieves it. Dockerfile reads and
replacements use `GET` and `PUT /v1/services/{id}/dockerfile`, with
`{"dockerfile":{"text":"FROM ..."}}` as the response and replacement
request envelope. PUT returns the saved text and
`X-Coast-Redeploy-Required: true`; it does not deploy.

## Deploy from a Compose file

Paste or upload UTF-8 Compose YAML in Horizon, or read a file with the CLI:

```console
$ openstack coast app create site --appgroup website --environment staging \
    --server worker-1 --compose-file ./compose.yaml --no-deploy
$ openstack coast compose show --service site
$ openstack coast deploy site
$ openstack coast service show site
$ openstack coast container list --service site
```

Although the creation command is `app create`, a pasted Compose becomes a
**service**. Find it in Services, with the same Compose editor and Containers
views as a template service. The file must describe the workload without
relying on local files that have not been uploaded to the worker.

The document must be a YAML mapping with a nonempty `services` mapping.
Duplicate keys and YAML aliases are rejected. Service and named-volume
names must start with a letter or digit and contain only letters, digits,
underscores, dots or hyphens. Service definitions must be mappings and
volume mounts must be lists of mount strings or mappings with a text
`target`. Invalid YAML returns HTTP 400 without echoing its contents.

To edit, save a replacement and deploy separately:

```console
$ openstack coast compose set --service site --file ./compose.yaml
$ openstack coast deploy site
```

The Compose editor's Save action also does not deploy. The existing API
1.12 `GET`/`PUT /v1/services/{id}/compose` routes read and replace desired
Compose. Ordinary service output omits the text; editor saves return the saved
`{"compose":{"yaml":"..."}}` envelope. Use
`compose show` or the editor's explicit read to retrieve it.

## Limits, ports and domains

Dockerfile and Compose text must be nonempty UTF-8. Both are bounded by
the smaller of the operator's `[api] max_compose_bytes` (default 10,000
bytes) and `[secrets] max_secret_bytes`. Oversized content returns HTTP
400. Text is stored in Barbican. Use environment secrets rather than
embedding credentials in source files; an explicit source read reveals
the complete saved text to authorized members.

For an HTTP app, set its container port with `--ports`, then follow
[Add a domain and verify it](client.md#add-a-domain-and-verify-it).
For Compose, configure container ports and any host-port publishing in
the YAML. Publishing a host port is separate from domain routing and
requires a matching stack ingress rule for direct external access.
Use the service's domain settings for HTTPS and publish the returned DNS
records before verifying the domain. A default URL alone does not mean
DNS and HTTPS are ready.

Workloads run unrestricted on your own workers. Coast validates Compose
structure and storage accounting; it does not sandbox Dockerfile
instructions or reject privileged containers, host networking, bind mounts
or Docker socket mounts as a workload safety policy. These settings can
give a workload control of its worker and affect other workloads on that
worker. Run source you trust and choose worker placement accordingly.

## API request fields

Create through `POST /v1/services` with the usual placement fields and
`source`. At API 1.30, an SSH Git source uses `type: git`, `repo_url` and
`deploy_key: {"generate": true}` or `deploy_key: {"private_key": "..."}`.
Responses expose `source.deploy_key.public_key` and
`source.deploy_key.fingerprint`, never the private key. Generated keys
default to `deploy: false`; direct API callers can explicitly request
`deploy: true` once repository access is configured. The CLI generated-key
workflow creates without deploying.

At API 1.31, use `source: {"type":"dockerfile","dockerfile":"<text>"}`
with `kind: app`, or `source: {"type":"compose","compose":"<text>"}`
with `kind: service`. Send UTF-8 source text directly to Coast, without
base64 encoding. Coast handles the app engine requests. Ordinary source
responses contain only the source type.

## API reference

For request fields, responses and errors, see the API reference sections
<a href="api-ref/services.html#private-git-deploy-keys-1-30">Private Git deploy keys</a>,
<a href="api-ref/services.html#repository-free-sources-1-31">Repository-free sources</a>,
<a href="api-ref/services.html#read-a-dockerfile">Read a Dockerfile</a>,
<a href="api-ref/services.html#replace-a-dockerfile">Replace a Dockerfile</a>
and the <a href="api-ref/versions.html">microversion history</a>.
The `tox -e docs` build includes the API reference in the `api-ref/`
subdirectory of the guide output. Publish or download the complete
`doc/build/html` directory to keep these links together.
