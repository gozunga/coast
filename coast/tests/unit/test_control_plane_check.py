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

import importlib.util
from pathlib import Path
import socket
from unittest import mock

from coast.tests import base


class GuestCheckTest(base.TestCase):
    def setUp(self):
        super().setUp()
        path = (Path(__file__).resolve().parents[3] /
                'tools/check-control-plane-isolation.py')
        spec = importlib.util.spec_from_file_location('guest_check', path)
        self.helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.helper)

    def test_bind_interface_and_close_on_timeout(self):
        with mock.patch.object(self.helper.socket, 'socket') as factory:
            sock = factory.return_value
            sock.connect.side_effect = TimeoutError()
            self.assertRaises(TimeoutError, self.helper.connect,
                              'env0', '192.0.2.1', 5672, 5)
            sock.setsockopt.assert_called_once_with(
                socket.SOL_SOCKET, socket.SO_BINDTODEVICE, b'env0\0')
            sock.close.assert_called_once_with()

    def test_open_control_plane_port_fails(self):
        connected = mock.MagicMock()
        connected.__exit__.return_value = False
        with mock.patch.object(self.helper, 'connect',
                               return_value=connected), \
                mock.patch('sys.argv', [
                    'guest-check', '--interface', 'env0',
                    '--control-plane-host', '192.0.2.1']):
            self.assertRaises(RuntimeError, self.helper.main)

    def test_refusal_does_not_prove_isolation(self):
        with mock.patch.object(self.helper, 'connect',
                               side_effect=ConnectionRefusedError()), \
                mock.patch('sys.argv', [
                    'guest-check', '--interface', 'env0',
                    '--control-plane-host', '192.0.2.1']):
            self.assertRaises(ConnectionRefusedError, self.helper.main)
