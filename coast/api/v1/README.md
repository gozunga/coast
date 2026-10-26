# API v1 controllers

Controllers validate requests with jsonschema (`schemas.py`), load
project-scoped versioned objects and check the policies in
`coast/policies/`. Async work goes through `coast/api/handlers.py`:
quota reservations share the resource write transaction, and the
handler then casts the operation to the conductor
(`coast/conductor/rpcapi.py`). Volume writes compare-and-swap
`status` because volume rows have no `task_state` column.
Routes are mounted in `coast/api/root_controller.py`.
