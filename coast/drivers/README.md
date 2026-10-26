# coast/drivers - PaaS engine drivers

`base.py` holds the `CoastDriver` ABC (see `doc/source/driver-interface.md`).
Drivers register under the stevedore namespace `coast.drivers` in
`setup.cfg`. `noop.py` is an in-memory fake used by unit/functional tests.
Drivers are pure translators: Coast desired state -> engine API calls.
They must not touch Nova/Neutron/Cinder (the conductor does that).
