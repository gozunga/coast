# coast/health_manager - `coast-health-manager`

`receiver.py` takes the Q79 signed UDP heartbeats, checks durable
sequence numbers and batches `last_heartbeat_at` updates. `keys.py`
resolves the per-stack Q86 Barbican key references; operators can
override the lookup. `stale.py` marks stale servers and casts worker
failover after commit; `failover.py` records health episodes and hands
failover to the conductor. `certificates.py` checks certificate lifetimes;
the conductor performs rotation.
See `doc/source/operations.md` and `doc/source/lifecycle.md`.
