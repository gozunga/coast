
Q108 mode-switch fixture
-----------------------

``build-mode.json`` contains synthetic, pinned-schema response subsets,
not live recordings. Worker roles are read from ``settings.server_role``;
the application build flag is read from ``settings.is_build_server_enabled``.
Tests apply documented PATCH request fields to these response shapes and
cover confirmation, repeated calls, lost replies and compensation.

M39 compose fixture
-------------------

``compose-edit.json`` is a synthetic public service response subset for the
selected release from ``coast/common/engine_versions.py``.
The test checks raw GET text and base64 PATCH with instant deployment off.
No generated template values or credentials are recorded.
