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

from unittest import mock

from coast.network import tags
from coast.tests import base


class CreateTaggedTest(base.TestCase):
    def test_create_then_set_tags(self):
        proxy = mock.Mock()
        created = mock.Mock()
        proxy.create_network.return_value = created
        proxy.set_tags.side_effect = lambda resource, values: resource
        result = tags.create_tagged(
            proxy, 'network', ['coast', 'project_id=tenant'],
            name='coast-env', is_shared=False)
        self.assertIs(created, result)
        self.assertNotIn('tags', proxy.create_network.call_args.kwargs)
        proxy.set_tags.assert_called_once_with(
            created, ['coast', 'project_id=tenant'])
        proxy.delete_network.assert_not_called()

    def test_tag_failure_deletes_each_kind(self):
        kinds = ('network', 'subnet', 'router', 'security_group', 'port',
                 'ip')
        for kind in kinds:
            proxy = mock.Mock()
            resource = mock.Mock()
            getattr(proxy, 'create_' + kind).return_value = resource
            proxy.set_tags.side_effect = RuntimeError('tag failed')
            self.assertRaises(
                RuntimeError, tags.create_tagged, proxy, kind, ['coast'],
                name='resource')
            create = getattr(proxy, 'create_' + kind)
            self.assertNotIn('tags', create.call_args.kwargs)
            getattr(proxy, 'delete_' + kind).assert_called_once_with(
                resource, ignore_missing=True)

    def test_delete_failure_keeps_the_tag_error(self):
        proxy = mock.Mock()
        proxy.create_port.return_value = mock.Mock()
        proxy.set_tags.side_effect = RuntimeError('tag failed')
        proxy.delete_port.side_effect = RuntimeError('delete failed')
        error = self.assertRaises(
            RuntimeError, tags.create_tagged, proxy, 'port', ['coast'],
            name='port')
        self.assertEqual('tag failed', str(error))

    def test_unknown_kind_creates_nothing(self):
        proxy = mock.Mock()
        self.assertRaises(
            ValueError, tags.create_tagged, proxy, 'security_group_rule',
            ['coast'])
        proxy.set_tags.assert_not_called()
