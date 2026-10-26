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

"""Development API server; production uses coast-wsgi-api."""

import socketserver
from wsgiref import simple_server

from oslo_log import log as logging
from oslo_utils import strutils

from coast.api import app
from coast.common import config


LOG = logging.getLogger(__name__)


# A client that opens a connection and never sends a full request must not
# block every other caller: serve each connection in its own thread and drop
# idle sockets after this many seconds.
REQUEST_TIMEOUT = 30


class RequestHandler(simple_server.WSGIRequestHandler):
    timeout = REQUEST_TIMEOUT

    def log_request(self, code='-', size='-'):
        # Request paths and queries can contain secrets; do not log them.
        LOG.info('API response: status=%s size=%s', code, size)

    def log_message(self, format, *args):
        LOG.info(strutils.mask_password(format % args))


class Server(socketserver.ThreadingMixIn, simple_server.WSGIServer):
    daemon_threads = True
    request_queue_size = 64


def main():
    config.parse_args()
    config.setup_logging()
    application = app.load_app()
    with simple_server.make_server(config.CONF.api.bind_host,
                                   config.CONF.api.bind_port,
                                   application,
                                   server_class=Server,
                                   handler_class=RequestHandler) as server:
        server.serve_forever()
    return 0
