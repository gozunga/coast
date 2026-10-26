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

from coast.common import config
from coast.health_manager import service as health_service


def main():
    config.parse_args()
    config.setup_logging()
    endpoint = health_service.Service()
    endpoint.prepare()
    launcher = service.launch(cfg.CONF, endpoint, workers=1)
    launcher.wait()
    return 0
