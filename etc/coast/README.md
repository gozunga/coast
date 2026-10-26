# etc/coast

- `coast.conf.sample` - generated option reference. `tox -e genconfig` runs
  `oslo-config-generator` with `etc/coast/coast-config-generator.conf`,
  using `coast.common.opts:list_opts` and the listed library namespaces.
  Copy it to an operator configuration file and supply deployment values.
- `api-paste.ini` - WSGI pipeline.
- `coast-config-generator.conf`, `coast-policy-generator.conf`.
- `POLICY.md` - policy conventions.
