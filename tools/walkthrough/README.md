# Quickstart CLI walkthrough

Run from the Coast checkout with Python 3.10 or later and the installed
python-coastclient plugin on PATH. No additional Python packages are used.
Load tenant OpenStack credentials using OpenRC or OS_CLOUD. Use an isolated
project with enough quota for a stack, worker and two workloads.

Export these operator-supplied inputs, then run:

```bash
export COAST_WALK_CONTROL_PLAN="$CONTROL_PLAN"
export COAST_WALK_WORKER_PLAN="$WORKER_PLAN"
export COAST_WALK_GIT_URL="$GIT_URL"
export COAST_WALK_GIT_BRANCH="$GIT_BRANCH"
export COAST_WALK_APP_PORT="$APP_PORT"
export COAST_WALK_TEMPLATE_ID="$TEMPLATE_ID"
python3 tools/walkthrough/quickstart.py
```

The git repository must have a Dockerfile and an HTTP app on the selected
port. Choose a visible template that can deploy with its defaults, or set
COAST_WALK_TEMPLATE_ENV_FILE to a private JSON file mapping required template
keys to string values. Values pass through stdin and are stored as secrets.
The script selects API 1.8. COAST_WALK_TIMEOUT defaults to 1800 seconds per
wait. Individual CLI calls have a 120-second limit. Do not enable CLI debug
logging or shell tracing. The script suppresses CLI diagnostics and logs.

Set COAST_WALK_DOMAIN to an owned hostname to include domain creation and
verification. The script pauses while you publish the returned DNS records
using your DNS provider. Without this input, the DNS step is skipped.
Inspect the resulting domain status and HTTPS response separately; the
script checks CLI completion, not external HTTP availability.

The script creates unique names and cleans up its stack in a finally block,
including after an error, Ctrl-C or SIGTERM. The API refuses to delete a
parent that still has children, so cleanup goes bottom-up: apps and services,
then environments (including the default production one), then app groups,
then the stack. Each delete waits until show returns 404, within
COAST_WALK_TIMEOUT. A 404 counts as already gone, so cleanup is safe to
repeat. A 409 from in-flight work is retried for up to 900 seconds. It then
waits until the stack leaves the list. A second Ctrl-C or SIGTERM during
cleanup is ignored. SIGKILL
still stops the process. It never deletes pre-existing stacks. A killed
process, lost create response or unreachable API can prevent cleanup. Keep
the printed stack UUID; inspect Activity, stack list and tagged cloud
resources after such a failure. Do not retry an uncertain create without
inspection.
Stack deletion destroys walkthrough data. DNS records remain yours to remove.

Offline tests cover bounded state waits, cleanup order, 404 and 409
handling, and cleanup on success and failure:

```bash
tox -e py3 -- coast.tests.unit.test_walkthrough
```

A successful offline test does not prove cloud deployment. Run the command
above against a configured cloud, then verify app HTTPS, domain status,
Activity and an empty stack list. No cloud credentials are stored here.
