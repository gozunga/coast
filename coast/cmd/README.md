# coast/cmd - console entry points

`api.py`, `conductor.py`, `health_manager.py`, `console_proxy.py`,
`agent.py` (guest agent), `manage.py` (operator commands), `db_manage.py`
and `status.py` (oslo.upgradecheck). Each exposes `main()` as wired in
`setup.cfg` `console_scripts`.
