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

import socket
import threading
import time
import unittest
import urllib.request
from wsgiref import simple_server

from coast.cmd import api


def _app(environ, start_response):
    start_response('200 OK', [('Content-Type', 'text/plain')])
    return [b'ok']


class StalledClientTest(unittest.TestCase):
    def test_stalled_connection_does_not_block_others(self):
        server = simple_server.make_server(
            '127.0.0.1', 0, _app, server_class=api.Server,
            handler_class=api.RequestHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        port = server.server_address[1]
        stalled = socket.create_connection(('127.0.0.1', port))
        self.addCleanup(stalled.close)
        stalled.sendall(b'GET / HTTP/1.1\r\n')  # never finished
        time.sleep(0.2)
        start = time.monotonic()
        body = urllib.request.urlopen(
            'http://127.0.0.1:%d/' % port, timeout=5).read()
        self.assertEqual(b'ok', body)
        self.assertLess(time.monotonic() - start, 3)

    def test_handler_has_read_timeout(self):
        self.assertTrue(api.RequestHandler.timeout)
        self.assertLessEqual(api.RequestHandler.timeout, 60)
