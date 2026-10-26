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

"""Version-matched metadata; never read a controller's template files."""

from importlib import resources
import json

from coast.common import engine_versions
from coast.drivers import base


VERSIONS = frozenset(engine_versions.SUPPORTED_ENGINE_VERSIONS)
# Templates the engine joins to its predefined network when created by
# type (NEEDS_TO_CONNECT_TO_PREDEFINED_NETWORK in the engine's
# bootstrap/helpers/constants.php at the TEMPLATE_VERSION tag). Raw compose
# creates must ask for this explicitly to keep the same behaviour.
PREDEFINED_NETWORK = frozenset(('pgadmin', 'databasus', 'redis-insight'))


def _read(version, kind):
    # Validate before building a resource path. Do not silently use another
    # engine version or fall back to an online engine request.
    if version not in VERSIONS:
        raise base.UnsupportedEngineVersion('Template catalog is unavailable')
    target = resources.files('coast.drivers.coolify').joinpath(
        'templates', '%s-%s.json' % (kind, engine_versions.TEMPLATE_VERSION))
    try:
        return json.loads(target.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        raise base.DriverError('Template catalog cannot be read') from None


def read(version):
    return _read(version, 'catalog')


def compose(version, template_id):
    """Return the pinned base64 compose for this engine version.

    The engine replaces its bundled template list with a CDN download every
    update check, so a template key valid at the pinned tag can disappear
    from a running engine. Coast therefore never creates templates by key.
    """
    try:
        return _read(version, 'compose')[template_id]
    except KeyError:
        raise base.EngineNotFound('Template compose was not found') from None
