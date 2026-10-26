# coast/conductor - `coast-conductor`

oslo.messaging cast consumer on `coast-conductor`, launched with
oslo.service (`manager.py`, client side in `rpcapi.py`). It loads the
stack's driver (`coolify_v4` or `noop`) and runs the operations that
change the cloud and the engine: stack create and teardown, server add,
remove, resize and failover, controller failover, engine upgrade, deploys,
domains and DNS, exports, database backups, volume delete, reconcile and
usage audit. Cloud calls use openstacksdk connections in the service
project (`coast/common/clients.py`); `cloud.py` is the tagged fake cloud
used on the noop driver path. Write operations release task locks, settle
quota reservations and record sanitized activity receipts. Irreversible
steps such as retained-volume deletion do not revert. No guest uses RPC.
Specs: `doc/source/lifecycle.md`, `doc/source/architecture.md`.
