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

"""Controller-only engine stop set using the shared durable export lease."""

import json

from coast.agent.engines import coolify
from coast.agent import export


class Quiesce(export.Quiesce):
    def validate(self, body, quiesce=False):
        if self.conf.agent.role != 'controller':
            raise coolify.PreconditionFailed()
        return super().validate(body, quiesce)

    def containers(self, deadline):
        selected = []
        for name in ('coolify', 'coolify-db'):
            output = self.command(
                'inspect', '--format',
                '{{json .Id}}|{{json .State.Running}}', name,
                timeout=min(30, max(0.01, deadline - self.clock()))
            ).decode()
            identifier, running = (json.loads(part)
                                   for part in output.split('|'))
            if (not export.CONTAINER_ID.fullmatch(identifier) or
                    not isinstance(running, bool)):
                raise coolify.PreconditionFailed()
            if running:
                selected.append(identifier)
        return selected
