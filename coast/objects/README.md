# coast/objects - oslo.versionedobjects

Versioned objects and ObjectListBase lists are defined in `resources.py`
and `backups.py`. Quota objects are data; quota enforcement is in
`coast/common/quota.py`. Secret carries only value_ref, never a secret
value.

Repository objects use the request context for get, list, create, save and
destroy operations. The DB API enforces tenant scope, status transitions,
immutable deployment fields and task-state compare-and-swap. Acquire and
release require an object without pending changes. Mutable JSON fields track
nested edits. Reference fields and JSON payloads are masked in object repr;
wire serialization retains the data for trusted control-plane services.

CoastObjectSerializer serializes objects on conductor RPC
(`coast/conductor/rpcapi.py`). Bump the object
version on any field or remotable-method change; the object-hash test in
`coast/tests/unit/test_objects.py` detects changes against expected hashes.
