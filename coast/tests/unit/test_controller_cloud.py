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

"""Nova boot mappings for image-backed servers with optional data disks."""

import subprocess
from types import SimpleNamespace
from unittest import mock

import fixtures
from openstack import exceptions as sdk_exceptions

from coast.conductor import controller
from coast.tests import base

VOLUME_ID = '1831dfbb-951f-4e73-b54d-c493a64a1a32'
SERIAL = '1831dfbb-951f-4e73-b'


class ControllerBootTest(base.TestCase):
    def setUp(self):
        super().setUp()
        self.cloud = controller.ControllerCloud(
            self.conf, SimpleNamespace(project_id='project', id='stack'))
        self.server = SimpleNamespace(
            id='server', hostname='coast-shop-5bad06-controller',
            image_id='image', flavor_id='flavor')
        self.proxy = mock.Mock()
        self.useFixture(fixtures.MockPatchObject(
            self.cloud, 'proxy', return_value=self.proxy))

    def boot_mapping(self, identifiers):
        volumes = [SimpleNamespace(cinder_volume_id=value)
                   for value in identifiers]
        self.cloud.boot(self.server, [], volumes, 'user-data')
        values = self.proxy.create_server.call_args.kwargs
        self.assertEqual(self.server.image_id, values['image_id'])
        return values['block_device_mapping']

    def test_no_volumes_has_empty_mapping(self):
        self.assertEqual([], self.boot_mapping([]))

    def check_volume_mapping(self, identifiers):
        expected = [dict(uuid='image', source_type='image',
                         destination_type='local', boot_index=0,
                         delete_on_termination=True)]
        expected.extend(dict(uuid=value, source_type='volume',
                             destination_type='volume', boot_index=-1,
                             delete_on_termination=False)
                        for value in identifiers)
        self.assertEqual(expected, self.boot_mapping(identifiers))

    def test_one_volume_has_image_first(self):
        self.check_volume_mapping(['data'])

    def test_two_volumes_have_image_first(self):
        self.check_volume_mapping(['data', 'docker'])

    def test_boot_uses_saved_hostname_and_attempt_tag(self):
        self.cloud.boot(self.server, [], [], 'data', attempt=VOLUME_ID)
        values = self.proxy.create_server.call_args.kwargs
        self.assertEqual(self.server.hostname, values['name'])
        self.assertIn('server_id=server', values['tags'])
        self.assertIn('attempt=' + VOLUME_ID, values['tags'])

    def test_delete_duplicate_names_only_for_owned_server(self):
        self.server.role = 'worker'
        self.server.compute_id = 'old'
        owned = self.cloud.tags + ['server_id=server']
        old = SimpleNamespace(id='old', name=self.server.hostname, tags=owned)
        new = SimpleNamespace(id='new', name=self.server.hostname, tags=owned)
        other = SimpleNamespace(id='other', name=self.server.hostname,
                                tags=self.cloud.tags + ['server_id=other'])
        self.proxy.servers.return_value = [old, new, other]
        self.proxy.get_server.return_value = old
        self.cloud.delete_server(self.server)
        self.assertEqual([mock.call(old, ignore_missing=True),
                          mock.call(new, ignore_missing=True)],
                         self.proxy.delete_server.call_args_list)

    def test_delete_rejects_stack_tags_without_server_identity(self):
        self.server.hostname = None
        self.server.compute_id = 'unnamed-id'
        guest = SimpleNamespace(id='unnamed-id', name='unnamed-name',
                                tags=list(self.cloud.tags))
        self.proxy.servers.return_value = []
        self.proxy.get_server.return_value = guest
        for role in ('control', 'worker', 'build'):
            with self.subTest(role=role):
                self.proxy.reset_mock()
                self.server.role = role
                self.assertRaisesRegex(
                    ValueError, 'Guest ownership differs',
                    self.cloud.delete_server, self.server)
                self.proxy.servers.assert_not_called()
                self.proxy.get_server.assert_called_once_with('unnamed-id')
                self.proxy.delete_server.assert_not_called()
                self.proxy.wait_for_delete.assert_not_called()

    def test_unnamed_delete_rejects_missing_or_conflicting_ownership(self):
        self.server.hostname = None
        self.server.compute_id = 'unnamed-id'
        self.server.role = 'worker'
        self.proxy.servers.return_value = []
        cases = [self.cloud.tags + ['server_id=another-server']]
        cases.extend(self.cloud.tags[:index] + self.cloud.tags[index + 1:]
                     for index in range(len(self.cloud.tags)))
        for tags in cases:
            with self.subTest(tags=tags):
                self.proxy.reset_mock()
                self.proxy.get_server.return_value = SimpleNamespace(
                    id='unnamed-id', name='unnamed-name', tags=tags)
                self.assertRaisesRegex(
                    ValueError, 'Guest ownership differs',
                    self.cloud.delete_server, self.server)
                self.proxy.delete_server.assert_not_called()
                self.proxy.wait_for_delete.assert_not_called()

    def test_unnamed_guest_already_gone_is_skipped(self):
        self.server.hostname = None
        self.server.compute_id = 'unnamed-id'
        self.server.role = 'worker'
        self.proxy.get_server.side_effect = sdk_exceptions.ResourceNotFound()
        self.cloud.delete_server(self.server)
        self.proxy.servers.assert_not_called()
        self.proxy.delete_server.assert_not_called()

    def test_unnamed_guest_without_saved_id_does_not_discover_by_name(self):
        self.server.hostname = None
        self.server.compute_id = None
        self.cloud.delete_server(self.server)
        self.proxy.servers.assert_not_called()
        self.proxy.get_server.assert_not_called()
        self.proxy.delete_server.assert_not_called()

    def test_named_guest_still_requires_server_ownership_tag(self):
        self.server.role = 'worker'
        self.server.compute_id = 'modern-id'
        self.proxy.servers.return_value = []
        self.proxy.get_server.return_value = SimpleNamespace(
            id='modern-id', tags=list(self.cloud.tags))
        self.assertRaisesRegex(ValueError, 'Guest ownership differs',
                               self.cloud.delete_server, self.server)
        self.proxy.delete_server.assert_not_called()

    def test_replacement_controller_is_deleted_by_recorded_cloud_id(self):
        self.server.role = 'control'
        self.server.compute_id = 'replacement-id'
        retained = {'replacement-id': SimpleNamespace(
            id='replacement-id', tags=self.cloud.tags + ['server_id=server'])}
        self.proxy.servers.return_value = []
        self.proxy.get_server.side_effect = retained.get
        self.proxy.delete_server.side_effect = lambda guest, **kwargs: (
            retained.pop(guest.id))
        self.cloud.delete_server(self.server)
        self.assertEqual({}, retained)


class GuestDiskPathTest(base.TestCase):
    def test_serial_is_twenty_characters(self):
        self.assertEqual(20, len(SERIAL))
        self.assertEqual(SERIAL, controller.disk_serial(VOLUME_ID))
        self.assertTrue(VOLUME_ID.startswith(SERIAL))

    def test_candidates_cover_both_buses_and_both_scsi_lengths(self):
        self.assertEqual((
            '/dev/disk/by-id/virtio-' + SERIAL,
            '/dev/disk/by-id/scsi-0QEMU_QEMU_HARDDISK_' + SERIAL,
            '/dev/disk/by-id/scsi-0QEMU_QEMU_HARDDISK_' + VOLUME_ID,
        ), controller.disk_candidates(VOLUME_ID))
        self.assertEqual(
            '/dev/disk/by-id/coast-' + VOLUME_ID,
            controller.disk_alias(VOLUME_ID))

    def test_link_script_mounts_alias_for_either_bus(self):
        volume = SimpleNamespace(
            cinder_volume_id=VOLUME_ID, purpose='data', mount_path='/data')
        script = controller.volume_link_script([volume])
        for path in controller.disk_candidates(VOLUME_ID):
            self.assertIn(path, script)
        self.assertIn(controller.disk_alias(VOLUME_ID), script)
        self.assertIn(
            'ENV{ID_SERIAL}=="%s"' % SERIAL, script)
        self.assertIn(
            'ENV{ID_SERIAL}=="0QEMU_QEMU_HARDDISK_%s"' % VOLUME_ID, script)
        self.assertIn('99-coast-volumes.rules', script)
        self.assertNotIn('/dev/disk/by-id/virtio-', script.split(
            'ENDCOASTRULES', 1)[0])
        self.assertNotIn('systemctl stop docker.service', script)
        self.assertNotIn('systemctl mask docker.socket', script)
        self.assertNotIn('systemctl mask docker.service', script)
        self.assertIn('link_disk ', script)
        checked = subprocess.run(
            ['sh', '-n'], input=script, text=True, capture_output=True,
            check=False)
        self.assertEqual(0, checked.returncode, checked.stderr)

    def test_two_volumes_each_get_a_link(self):
        second = '51b39d45-a8a5-43d7-b5f7-7dbf195d4014'
        volumes = [
            SimpleNamespace(cinder_volume_id=VOLUME_ID, purpose='data',
                            mount_path='/data'),
            SimpleNamespace(cinder_volume_id=second, purpose='docker',
                            mount_path='/var/lib/docker')]
        script = controller.volume_link_script(volumes)
        self.assertIn(controller.disk_alias(VOLUME_ID), script)
        self.assertIn(controller.disk_alias(second), script)
        self.assertEqual(2, script.count('link_disk '))

    def test_unsafe_volume_id_is_rejected(self):
        for value in ('not-a-uuid', "1831dfbb-951f-4e73-b54d-c493a64a1a32'",
                      '', None, VOLUME_ID.upper() + 'x'):
            self.assertRaises(ValueError, controller.disk_serial, value)

    def test_duplicate_guest_serial_is_rejected(self):
        # The 20-character virtio serial is shared. Two links would race.
        other = SERIAL + '222-222222222222'
        self.assertEqual(SERIAL, controller.disk_serial(other))
        volumes = [
            SimpleNamespace(cinder_volume_id=VOLUME_ID),
            SimpleNamespace(cinder_volume_id=other)]
        self.assertRaises(
            ValueError, controller.volume_link_script, volumes)
