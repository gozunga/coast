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

"""Offline checks of the live contract fixtures (#56 acceptance run 7)."""

import ast
from pathlib import Path

from coast.tests import base
from coast.tests.contract.coolify import test_live

LIVE = Path(test_live.__file__)


class EngineAppFixtureTest(base.TestCase):

    def test_engine_app_has_production_defaults(self):
        app = test_live.engine_app(id="a", engine_ref=None)
        self.assertIsNone(app.template_id)
        self.assertEqual("a", app.id)
        app = test_live.engine_app(template_id="wordpress-with-mariadb")
        self.assertEqual("wordpress-with-mariadb", app.template_id)

    def test_every_engine_app_fixture_uses_the_factory(self):
        # An app or service handed to the driver carries server_engine_ref.
        # Built as a bare SimpleNamespace it lacks template_id, and the
        # driver raised AttributeError before reaching the engine.
        bare = []
        factory = 0
        for node in ast.walk(ast.parse(LIVE.read_text())):
            if not isinstance(node, ast.Call):
                continue
            keys = {k.arg for k in node.keywords}
            if "server_engine_ref" not in keys:
                continue
            name = getattr(node.func, "id", getattr(node.func, "attr", ""))
            if name == "engine_app":
                factory += 1
            else:
                bare.append((name, node.lineno))
        self.assertEqual([], bare)
        self.assertGreaterEqual(factory, 4)
