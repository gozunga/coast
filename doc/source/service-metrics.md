# Service CPU and memory history

API 1.21 adds `GET /v1/services/{service_id}/metrics`. A project reader can
read its service history. An administrator can read any service history.
A resource in another project returns 404 to a non-admin caller.
This endpoint supports applications and services, including each container
in a compose service. It does not require a running service to read history.

Set the `OpenStack-API-Version: app-hosting 1.21` header. Use the `range`
query parameter to select the time window. The default is `1h`.

| Range | Step in seconds |
| --- | --- |
| `15m`, `1h`, `6h`, `24h` | 30 |
| `7d` | 300 |
| `30d` | 3600 |

The response has a `metrics` object with `service_id`, `range`,
`step_seconds` and `containers`. Each container has a `name` and `points`.
Each point has a UTC `timestamp`, `cpu_percent`, `memory_bytes` and
`memory_limit_bytes`. Points are ordered from oldest to newest.
Names are the compose container service name, or the Docker container name
when there is no compose name or replicas share that compose name. Stopped or missing containers have no new
points. A lost heartbeat or a failed sample leaves a gap. Empty history
returns `containers: []`. Coast does not fill gaps with zero values.

CPU is the percentage of the CPUs available to the container. The CPU
quota and CPU set restrict that value. Without a limit, all host CPUs are
available. A short sample can exceed 100% when CPU use bursts above its
quota. Memory is usage minus inactive page cache, as in `docker stats`.
An unlimited memory setting returns `memory_limit_bytes: null`.
Coast collects no network, restart, disk or server graph statistics here.

Each UTC bucket contains an average weighted by the number of samples.
Byte averages round down to whole bytes. A memory limit is null if any
sample in that bucket was unlimited. Larger windows include recent detail
aggregated to their requested step. Buckets can start just before the
requested window if that window starts partway through a bucket.
Read this history with `openstack coast metrics show --service web --range 1h`;
`--service` names the app or service and `--range 1h` requests the last hour.
The Horizon app and service detail pages also provide metrics.
