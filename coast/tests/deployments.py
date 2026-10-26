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

"""SQLite, fake RPC and live/synthetic Coolify recovery fixtures."""

import copy
import datetime
import json
from pathlib import Path
from unittest import mock

from oslo_utils import timeutils
import requests_mock

from coast.common import states
from coast.conductor import rpcapi
from coast import objects
from coast.tests import conductor


class DeploymentTest(conductor.ConductorTest):
    def setUp(self):
        super().setUp()
        path = self.stack()
        self.env = objects.Stack.get_by_id(self.ctx, path.split('/')[-1])
        self.worker = next(row for row in objects.ServerList.get_all(self.ctx)
                           if row.role == 'worker')
        created = self.request('post', '/v1/services',
                               self.service_body(False),
                               status=202, complete=True).json['service']
        self.service = objects.Service.get_by_id(self.ctx, created['id'])
        self.service.engine_ref = 'app-uuid'
        self.service.health_check = 'http'
        self.service.save()
        self.env.driver = 'coolify_v4'
        self.env.engine_version = '4.4.3'
        self.env.engine_ref = 'project-uuid'
        ref = 'https://secrets.example.com/opaque'
        self.store.values[ref] = (self.ctx.project_id, 'engine-secret')
        self.env.engine_token_ref = ref
        self.env.save()
        self.config.config(enabled_drivers=['noop', 'coolify_v4'],
                           group='controller_worker')
        self.url = 'https://engine.example.com/api/v1'
        self.config.config(insecure_direct_url=True,
                           adopted_endpoints={self.env.id:
                                              'https://engine.example.com'},
                           retry_attempts=1, retry_wait=0,
                           # Keep the held final 429 retry instant.
                           rate_limit_timeout=0.5, group='coolify')
        disks = [row for row in objects.VolumeList.get_all(
            self.ctx, filters={'server_id': self.worker.id,
                               'purpose': 'docker'})
                 if row.status != states.DELETING]
        if not disks:
            objects.Volume(
                context=self.ctx, stack_id=self.env.id,
                server_id=self.worker.id, name='docker', size_gb=20,
                mount_path='/var/lib/docker', purpose='docker',
                status=states.AVAILABLE).create()
        self.http = requests_mock.Mocker()
        self.http.start()
        self.addCleanup(self.http.stop)
        path = (Path(__file__).parent /
                'unit/drivers/coolify/fixtures/v4.4.3.json')
        self.fixtures = json.loads(
            path.with_name("synthetic.json").read_text())
        self.fixtures.update(json.loads(path.read_text()))
        self.http.get(self.url + '/applications/app-uuid/envs', json=[])
        self.http.get(self.url + '/applications/app-uuid/storages', json={})
        self.http.patch(self.url + '/applications/app-uuid', json={})
        # A finished deployment is ACTIVE only once the app runs.
        self.http.get(self.url + '/applications/app-uuid',
                      json={'uuid': 'app-uuid', 'status': 'running:healthy'})

        def deploy_body(request, context):
            # The recorded fixture names the setup appgroup. A real
            # enqueue returns the appgroup uuid from the request.
            body = copy.deepcopy(self.fixtures['deploy'])
            requested = request.qs.get('uuid', [None])[0]
            rows = body.get('deployments') or []
            if requested and len(rows) == 1:
                rows[0]['resource_uuid'] = requested
            return body

        self.enqueue = self.http.post(self.url + '/deploy', json=deploy_body)
        self.status = self.http.get(self.url + '/deployments/deployment-uuid',
                                    json=self.fixtures['deployment'])

    def service_body(self, deploy=True, name='web'):
        return {'service': {**self.service_parents(self.worker.stack_id),
                            'name': name, 'server_id': self.worker.id,
                            'deploy': deploy, 'source': {
                                'type': 'git',
                                'repo_url': 'https://example.com/web.git',
                                'build_pack': 'nixpacks',
                                'base_directory': '.'}}}

    def accepted(self, action=None):
        messages = []

        def capture(ctx, operation, resource, options=None):
            messages.append(dict(operation=operation,
                                 resource=resource.obj_clone(),
                                 options=options or {},
                                 revision=rpcapi.revision(resource),
                                 reservation=copy.deepcopy(getattr(
                                     ctx, 'quota_reservation', {}))))

        path = '/v1/services/' + self.service.id + '/deployments'
        with mock.patch.object(self.rpc, 'cast', side_effect=capture):
            if action:
                row = self.request('post', path + '/' + action + '/action',
                                   {'rollback': {}}, status=202).json[
                                       'deployment']
            else:
                row = self.request('post', path, {'deployment': {}},
                                   status=202).json['deployment']
        return row['id'], messages[0]

    def age(self, deployment_id):
        row = objects.Deployment.get_by_id(self.ctx, deployment_id)
        row.started_at = timeutils.utcnow() - datetime.timedelta(
            seconds=self.conf.conductor.deploy_timeout + 1)
        row.save()

    def settled(self, deployment_id, status):
        row = objects.Deployment.get_by_id(self.ctx, deployment_id)
        self.assertEqual(status, row.status)
        self.assertIsNotNone(row.finished_at)
        service = objects.Service.get_by_id(self.ctx, self.service.id)
        env = objects.Stack.get_by_id(self.ctx, self.env.id)
        self.assertEqual(states.NONE, service.task_state)
        self.assertEqual(states.NONE, env.task_state)
        self.assertEqual(0, self.usage()['reserved_apps'])
        self.assertEqual(1, self.usage()['in_use_apps'])
        return row
