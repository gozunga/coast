# coast/common - shared plumbing

`config.py`/`opts.py` (oslo.config options + `list_opts` for genconfig),
`context.py` (RequestContext), `exception.py`, `policy.py` (enforcer),
`notifications.py` (oslo.messaging lifecycle notifier), `clients.py`
(cached openstacksdk connections in the service project), `secrets.py`
(secret store backends), `quota.py`, `engine_versions.py` (supported
engine releases), `tls.py` (Coast CA, cert issuance), `states.py`
(status enums). RPC client code is in `coast/conductor/rpcapi.py`.
