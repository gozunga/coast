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

from concurrent import futures
import threading
from types import SimpleNamespace
from unittest import mock

from coast.common import template_catalog
from coast.tests import base


class TemplateCatalogTest(base.TestCase):
    def setUp(self):
        super().setUp()
        self.patch = mock.patch.object(template_catalog.cfg, 'CONF', self.conf)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        template_catalog._CACHE.clear()
        self.addCleanup(template_catalog._CACHE.clear)
        self.stack = SimpleNamespace(
            driver='coolify_v4', engine_version='4.4.3')
        patch = mock.patch.object(template_catalog.loader, 'load_driver')
        self.driver = patch.start().return_value
        self.addCleanup(patch.stop)
        self.driver.list_templates.return_value = [{'id': 'one', 'tags': []}]
        patch = mock.patch.object(template_catalog.time, 'monotonic',
                                  return_value=100)
        self.clock = patch.start()
        self.addCleanup(patch.stop)

    def test_expiry_and_caller_mutation(self):
        self.config.config(cache_ttl=10, group='templates')
        first = template_catalog.read(self.stack)
        first[0]['tags'].append('changed')
        self.driver.list_templates.return_value = [{'id': 'two'}]
        self.clock.return_value = 109
        self.assertEqual([{'id': 'one', 'tags': []}],
                         template_catalog.read(self.stack))
        self.clock.return_value = 110
        self.assertEqual([{'id': 'two'}], template_catalog.read(self.stack))

    def test_disabled_cache_discards_previous_entry(self):
        template_catalog.read(self.stack)
        self.config.config(cache_ttl=0, group='templates')
        self.driver.list_templates.return_value = [{'id': 'two'}]
        self.assertEqual([{'id': 'two'}], template_catalog.read(self.stack))
        self.driver.list_templates.return_value = [{'id': 'three'}]
        self.assertEqual([{'id': 'three'}], template_catalog.read(self.stack))
        self.config.config(cache_ttl=3600, group='templates')
        self.assertEqual([{'id': 'three'}], template_catalog.read(self.stack))

    def test_driver_and_version_keys(self):
        template_catalog.read(self.stack)
        self.driver.list_templates.return_value = [{'id': 'other'}]
        for stack in (SimpleNamespace(driver='noop', engine_version='4.4.3'),
                      SimpleNamespace(driver='coolify_v4',
                                      engine_version='5')):
            self.assertEqual([{'id': 'other'}], template_catalog.read(stack))
        self.assertEqual([{'id': 'one', 'tags': []}],
                         template_catalog.read(self.stack))

    def test_failed_read_is_not_cached(self):
        self.driver.list_templates.side_effect = ValueError('unavailable')
        self.assertRaises(ValueError, template_catalog.read, self.stack)
        self.driver.list_templates.side_effect = None
        self.assertEqual([{'id': 'one', 'tags': []}],
                         template_catalog.read(self.stack))

    def test_concurrent_misses_share_one_fill(self):
        barrier = threading.Barrier(8)

        def load(*args):
            return [{'id': str(threading.get_ident())}]

        def read():
            barrier.wait(timeout=5)
            return template_catalog.read(self.stack)

        self.driver.list_templates.side_effect = load
        with futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda unused: read(), range(8)))
        self.assertTrue(results[0])
        for result in results:
            self.assertEqual(results[0], result)
        self.driver.list_templates.assert_called_once()
