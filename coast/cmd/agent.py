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

from oslo_config import cfg
from oslo_service import service

from coast.agent import server
from coast.common import config


def main():
    # Do not monkey-patch sockets: setns must stay on a native thread and
    # engine connections must be created after its namespace is restored.
    config.parse_args()
    config.setup_logging()
    launcher = service.launch(cfg.CONF, server.Service(cfg.CONF), workers=1)
    launcher.wait()
    return 0
