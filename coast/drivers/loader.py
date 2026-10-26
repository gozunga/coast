# Copyright 2026 Gozunga and Coast contributors
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""Load the engine driver selected by a stack."""

from stevedore import driver

from coast.drivers import base


def _load_failure(manager, entry_point, error):
    raise base.DriverError('Engine driver could not be loaded') from None


def load_driver(name, conf):
    """Load one plugin without silently falling back to another engine."""
    try:
        loaded = driver.DriverManager(
            'coast.drivers', name, invoke_on_load=True,
            invoke_kwds={'conf': conf},
            on_load_failure_callback=_load_failure).driver
    except Exception:
        raise base.DriverError('Engine driver could not be loaded') from None
    if not isinstance(loaded, base.CoastDriver):
        raise base.DriverError('Engine driver does not implement CoastDriver')
    if not loaded.SUPPORTED_ENGINE_VERSIONS:
        raise base.UnsupportedEngineVersion(
            'Engine driver declares no versions')
    return loaded
