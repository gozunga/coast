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

"""Recapture portal JSON using isolated functional fixtures, never a cloud.

Run through tox: tox -e doc-examples [-- --write]
Requests come from the guide and pass through the real WSGI validators.
Response bodies are never constructed here. Fixture completions below simulate
asynchronous work; this checks the public API contract, not engine behaviour.
Only identifiers, timestamps, DNS challenges and bearer URLs are normalized.
"""

import argparse
import datetime
import json
from pathlib import Path
import re
from unittest import mock

from oslo_policy import policy as policy_module

from coast.api import handlers
from coast.common import engine_versions
from coast.common import policy
from coast.common import states
from coast import objects
from coast.tests import app_api


GUIDE = Path('doc/source/portal-guide.md')
BLOCK = re.compile(r'```json\n(.*?)\n```', re.S)
UUID = re.compile(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}')
STAMP = re.compile(r'\d{4}-\d\d-\d\dT[0-9:.]+Z?')
VERSION = '1.28'


class Capture(app_api.AppAPITest):
    """Share the in-process API fixture used by functional tests."""

    def configure(self, text):
        self.text = text
        self.matches = list(BLOCK.finditer(text))
        self.examples = [json.loads(m[1]) for m in self.matches]
        self.output = {}
        self.ids = {}
        self.config.config(driver='noop', group='controller_worker')
        self.config.config(public_endpoint='https://api.example.com',
                           group='api')
        self.config.config(default_suffix='apps.example.com', group='domains')
        self.config.config(environments=5, servers=10, apps=10,
                           volume_gb=1000, group='quotas')
        self.config.config(definitions=json.dumps([
            {'name': 'example-control', 'kind': 'vm', 'roles': ['control'],
             'description': 'Example controller plan', 'flavor': 'control'},
            {'name': 'example-worker', 'kind': 'vm',
             'roles': ['worker', 'build'],
             'description': 'Example worker plan', 'flavor': 'worker'}]),
            group='plans')
        self.config.config(public_endpoint='console.example.com:9610',
                           group='console')
        # Suppress only asynchronous RPC handoff, never views or validation.
        network = mock.patch('socket.socket.connect', side_effect=RuntimeError(
            'Network is disabled during documentation capture.'))
        network.start()
        self.addCleanup(network.stop)
        self.cast = mock.patch('coast.conductor.rpcapi.ConductorAPI.cast')
        self.cast.start()
        self.addCleanup(self.cast.stop)

    def translate(self, value, reverse=False):
        mapping = ({v: k for k, v in self.ids.items()}
                   if reverse else self.ids)
        raw = json.dumps(value)
        for old, new in mapping.items():
            raw = raw.replace(old, new)
        return json.loads(raw)

    def bind(self, old, new):
        # Preserve the guide's stable example IDs, never its response fields.
        if isinstance(old, dict) and isinstance(new, dict):
            for key in old.keys() & new.keys():
                self.bind(old[key], new[key])
        elif isinstance(old, list) and isinstance(new, list):
            for left, right in zip(old, new):
                self.bind(left, right)
        elif (isinstance(old, str) and UUID.fullmatch(old) and
              isinstance(new, str) and UUID.fullmatch(new)):
            if old not in self.ids and new not in self.ids.values():
                self.ids[old] = new

    def normalize(self, value):
        value = self.translate(value, reverse=True)
        raw = json.dumps(value)
        for stable, actual in self.ids.items():
            raw = raw.replace('-' + actual[:4] + '.',
                              '-' + stable[:4] + '.')
            raw = raw.replace('-' + actual[:6] + '-',
                              '-' + stable[:6] + '-')
        raw = STAMP.sub('2026-10-22T12:00:00Z', raw)
        raw = re.sub(r'(wss://[^" ]+\?token=)[^" ]+',
                     r'\1REDACTED', raw)
        value = json.loads(raw)
        # Engine releases are owned by engine_versions, not documentation.

        def engine(item):
            if (isinstance(item, dict) and
                    item.get('purpose') == 'verification'):
                item = dict(item, value='example-dns-challenge')
            if isinstance(item, dict):
                result = {k: ('<engine version>' if k == 'engine_version'
                              else engine(v)) for k, v in item.items()}
                if result.get('object_expires_at'):
                    result['object_expires_at'] = '2026-10-25T12:00:00Z'
                if result.get('expires_at'):
                    result['expires_at'] = ('2026-10-22T12:01:00Z'
                                            if 'url' in result else
                                            '2026-10-22T13:00:00Z')
                return result
            if isinstance(item, list):
                return [engine(v) for v in item]
            return item
        return engine(value)

    def call(self, index, request_index=None, status=200, path=None,
             method=None, headers=None):
        prefix = self.text[:self.matches[index].start()]
        routes = re.findall(r'^(GET|POST|PATCH|PUT|DELETE) (\S+)',
                            prefix, re.M)
        verb, route = routes[-1]
        route = self.translate(path or route)
        body = (self.translate(self.examples[request_index])
                if request_index is not None else None)
        response = self.request((method or verb).lower(), route, body,
                                version=VERSION, status=status,
                                headers=headers)
        self.bind(self.examples[index], response.json)
        self.output[index] = self.normalize(response.json)
        if request_index is not None:
            self.output[request_index] = self.normalize(body)
        return response.json

    def row(self, cls, example_id):
        return cls.get_by_id(self.ctx, self.ids.get(example_id, example_id))

    def complete(self, cls, example_id, *statuses, **values):
        row = self.row(cls, example_id)
        for status in statuses:
            row.status = status
            row.save()
        if hasattr(row, 'task_state') and row.task_state != states.NONE:
            row.release_task(row.task_state)
        for key, value in values.items():
            setattr(row, key, value)
        row.save()
        return row

    def run_capture(self):
        self.call(0)
        self.call(1)
        self.call(3)
        created = self.call(5, 4, status=202)['stack']
        stack = self.examples[5]['stack']['id']
        self.complete(objects.Stack, stack, states.BUILDING, states.ACTIVE)
        self.call(6)
        # The fake controller has no Nova. Allocate fixture servers locally.
        servers = [
            ('149b064d-5436-5282-a998-23d731f5b797', 'control-0', 'control'),
            ('43828f3a-dd3f-5308-b8eb-51f304689211', 'worker-1', 'worker')]
        for stable_id, name, role in servers:
            server = objects.Server(
                context=self.ctx, stack_id=created['id'], name=name,
                kind='vm', role=role, status=states.ACTIVE,
                task_state=states.NONE)
            server.create()
            self.ids[stable_id] = server.id
        self.call(7)
        self.call(9, 8, status=202)
        worker = self.examples[9]['server']['id']
        self.complete(objects.Server, worker, states.BUILDING, states.ACTIVE)
        self.call(10)
        self.call(11, status=202)
        # A separate pre-existing stack for nested parent examples.
        self.ids['5a814ad0-d4a5-4480-97cd-aec1e3f8b847'] = self.stack.id
        self.ids['2b3f2ad6-8d5d-4116-91ac-090a26a0be3c'] = self.server.id
        self.application.name = 'fixture-website'
        self.application.save()
        self.application = objects.Appgroup(
            context=self.ctx, stack_id=created['id'], name='website',
            status=states.ACTIVE, task_state=states.NONE,
            engine_ref='example-main-group')
        self.application.create()
        self.environment = objects.Environment(
            context=self.ctx, appgroup_id=self.application.id,
            name='production', status=states.ACTIVE, task_state=states.NONE,
            engine_ref='example-main-environment')
        self.environment.create()
        self.call(13, 12, status=202)
        group = self.examples[13]['appgroup']['id']
        self.complete(objects.Appgroup, group, states.ACTIVE,
                      engine_ref='example-group')
        self.call(15, 14, status=202)
        environment = self.examples[15]['environment']['id']
        self.complete(objects.Environment, environment, states.ACTIVE,
                      engine_ref='example-environment')
        self.ids['1422cb40-ae97-4440-b05b-d0b0ea4a63fd'] = (
            self.application.id)
        self.ids['548c975a-4805-494f-b555-a54984ea408e'] = self.environment.id
        self.call(17, 16, status=202)
        service = self.examples[17]['service']['id']
        deployment = self.examples[17]['service']['deployment_id']
        self.complete(objects.Deployment, deployment, states.DEPLOYING,
                      states.ACTIVE, resolved_ref='0' * 40)
        self.complete(objects.Service, service, states.ACTIVE,
                      current_deployment_id=self.ids[deployment],
                      engine_ref='example-app')
        self.call(18)
        self.call(19)
        # Image-only request example still goes through the real API.
        body = self.translate(self.examples[20])
        self.request('post', '/v1/services', body, status=202, version=VERSION)
        self.output[20] = self.normalize(body)
        # Read the pinned catalog through the public API with an allowlist.
        self.stack.engine_version = engine_versions.TEMPLATE_VERSION
        self.stack.save()
        self.stack.driver = 'coolify_v4'
        self.stack.save()
        enforcer = policy.get_enforcer()
        enforcer.load_rules()
        enforcer.set_rules(policy_module.Rules.from_dict({
            'coast:template:get': "'wordpress-with-mariadb':%(template_id)s"}),
            overwrite=False)
        self.call(21)
        self.call(22)
        objects.Volume(
            context=self.ctx, stack_id=self.stack.id,
            server_id=self.server.id, name='docker', size_gb=100,
            purpose='docker', mount_path='/var/lib/docker',
            status=states.AVAILABLE).create()
        self.call(24, 23, status=202)
        self.domain.destroy()
        self.call(26, 25, status=201)
        domain = self.examples[26]['domain']['id']
        self.unlock(self.ids[service])
        self.call(28, 27, status=202)
        self.complete(objects.Domain, domain, states.VERIFIED,
                      verified_at=datetime.datetime(2026, 10, 22, 12))
        self.unlock(self.ids[service])
        self.call(29)
        self.call(30, status=202)
        self.unlock(self.ids[service])
        self.call(31)
        self.call(33, 32)
        self.call(35, 34)
        self.call(36)
        self.call(37)
        self.call(39, 38, status=202)
        second = self.examples[39]['deployment']['id']
        self.complete(objects.Deployment, deployment, states.SUPERSEDED)
        self.complete(objects.Deployment, second, states.DEPLOYING,
                      states.ACTIVE)
        self.complete(objects.Service, service,
                      current_deployment_id=self.ids[second])
        self.call(40)
        self.call(42, 41, status=202)
        self.unlock(self.ids[service])
        # Cancellation has its own active service and pending deployment.
        self.ids['ba9a7b9a-9987-418a-af57-2c50d58454fb'] = self.service.id
        self.deployment.status = states.SUPERSEDED
        self.deployment.save()
        pending = objects.Deployment(
            context=self.ctx, service_id=self.service.id,
            server_id=self.server.id, spec={'env': {}}, trigger='api',
            status=states.PENDING)
        pending.create()
        self.service.acquire_task(states.DEPLOYING)
        self.ids['72ce1873-4e1d-4a7d-a419-bd5dc593bbdf'] = pending.id
        self.call(44, 43, status=202)
        self.call(46, 45, status=202)
        self.complete(objects.Service, service, states.STOPPED)
        self.call(48, 47, status=202)
        self.complete(objects.Service, service, states.ACTIVE)
        self.call(50, 49, status=202)
        self.unlock(self.ids[service])
        self.call(52, 51, status=400)
        self.config.config(apps=0, group='quotas')
        self.call(82, 81, status=403)
        self.config.config(apps=10, group='quotas')
        self.call(54, 53, status=202)
        volume = self.examples[54]['volume']['id']
        self.complete(objects.Volume, volume, states.AVAILABLE)
        self.call(55)
        self.call(57, 56, status=202)
        export = self.examples[57]['export']['id']
        self.complete(objects.Export, export, states.SNAPSHOTTING,
                      states.UPLOADING, states.ACTIVE)
        self.complete(objects.Stack, stack)
        self.call(58)
        self.call(60, 59, status=409)
        self.capture_archive()
        self.call(67, status=202)
        # Shell grant precedes deletion; no guest is contacted.
        shell_server = self.row(objects.Server,
                                '43828f3a-dd3f-5308-b8eb-51f304689211')
        shell_server.mgmt_ip = '198.51.100.10'
        shell_server.save()
        self.call(80, 79, status=201,
                  path='/v1/apps/' + self.ids[service] + '/console')
        self.call(68, status=202)
        deleted = self.row(objects.Service, service)
        deleted.destroy()
        self.call(69, status=404)
        self.complete(objects.Service, self.examples[24]['service']['id'],
                      states.STOPPED)
        self.row(objects.Service, self.examples[24]['service']['id']).destroy()
        self.call(70, status=202)
        self.row(objects.Environment, environment).destroy()
        for child in objects.EnvironmentList.get_all(
                self.ctx, filters={'appgroup_id': self.ids[group]}):
            child.destroy()
        self.call(71, status=202)
        self.capture_volume_delete()
        for parent in objects.AppgroupList.get_all(self.ctx):
            parent.destroy()
        for workload in objects.ServiceList.get_all(
                self.ctx, filters={'stack_id': created['id']}):
            if workload.task_state != states.NONE:
                workload.release_task(workload.task_state)
            workload.destroy()
        self.call(76, 75, status=202)
        self.row(objects.Stack, stack).destroy()
        self.call(77, status=404)
        # Snapshot delete request is validated on another empty fixture stack.
        for workload in objects.ServiceList.get_all(
                self.ctx, filters={'stack_id': self.stack.id}):
            if workload.task_state != states.NONE:
                workload.release_task(workload.task_state)
            workload.destroy()
        body = self.examples[78]
        self.request('delete', '/v1/stacks/' + self.stack.id, body,
                     version=VERSION, status=202)
        self.output[78] = self.normalize(body)
        self.call(83)
        self.call(84)
        # Event fixture uses the public read view with a deterministic entry.
        event = objects.Event(
            context=self.ctx, stack_id=created['id'], resource_type='app',
            resource_id=self.ids[service], action='create', outcome='accepted',
            message='Create accepted.')
        event.create()
        self.call(2)
        self.assertEqual(set(range(len(self.matches))), set(self.output))

    def capture_archive(self):
        stack = objects.Stack(context=self.ctx, name='archive', plan='small',
                              driver='noop',
                              engine_version=engine_versions.DEFAULT,
                              status=states.ACTIVE, task_state=states.NONE)
        stack.create()
        self.ids['af53612d-fc4a-48a2-a18e-8292d8cf1987'] = stack.id
        volume = objects.Volume(context=self.ctx, stack_id=stack.id,
                                name='data', size_gb=2, purpose='data',
                                status=states.AVAILABLE)
        volume.create()
        self.ids['c8e24b2b-7a60-48e5-8afb-882131f46efd'] = volume.id
        self.call(62, 61, status=202)
        export = self.examples[62]['export']['id']
        expiry = datetime.datetime.now(datetime.timezone.utc) + (
            datetime.timedelta(days=3))
        self.complete(objects.Export, export, states.SNAPSHOTTING,
                      states.UPLOADING, states.ACTIVE,
                      object_expires_at=expiry, artifacts=[{
                          'volume_id': volume.id, 'name': 'data.tar.gz',
                          'size_bytes': 42, 'sha256': 'a' * 64,
                          'object_key': 'private-object'}])
        urls = [{'volume_id': volume.id,
                 'download_url': ('https://objects.example.com/archive'
                                  '?temp_url_sig=REDACTED'),
                 'expires_at': expiry - datetime.timedelta(days=2)}]
        with mock.patch.object(handlers.Handler, 'export_urls',
                               return_value=urls):
            self.call(63)
            self.call(64, headers=dict(self.headers, **{'X-Roles': 'member'}))
            self.call(66, 65)

    def capture_volume_delete(self):
        stack = objects.Stack(context=self.ctx, name='delete-volume',
                              plan='small',
                              driver='noop',
                              engine_version=engine_versions.DEFAULT,
                              status=states.ACTIVE,
                              task_state=states.RECONCILING)
        stack.create()
        self.ids['be497d22-0933-4709-85db-2c5b7efbd0d9'] = stack.id
        volume = objects.Volume(context=self.ctx, stack_id=stack.id,
                                name='saved-data', size_gb=2, purpose='data',
                                status=states.AVAILABLE)
        volume.create()
        self.ids['a639618e-55c0-464e-88e6-5d11b94785fa'] = volume.id
        self.call(72, status=409)
        stack.release_task(states.RECONCILING)
        self.call(73, status=202)
        volume.destroy()
        self.call(74, status=404)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--write', action='store_true',
                        help='replace JSON blocks with captured output')
    args = parser.parse_args()
    text = GUIDE.read_text()
    versions = re.findall(r'OpenStack-API-Version: app-hosting (1\.\d+)',
                          text)
    if not versions or any(version != VERSION for version in versions):
        raise SystemExit('Portal request headers must select API ' + VERSION)
    fixture = Capture()
    fixture.setUp()
    try:
        fixture.configure(text)
        fixture.run_capture()
        differences = [i for i, result in fixture.output.items()
                       if fixture.examples[i] != result]
        if args.write:
            for index, match in reversed(list(enumerate(fixture.matches))):
                rendered = json.dumps(fixture.output[index], indent=2)
                text = text[:match.start(1)] + rendered + text[match.end(1):]
            GUIDE.write_text(text)
        elif differences:
            raise SystemExit('Portal JSON differs at blocks: ' +
                             ', '.join(str(i) for i in sorted(differences)))
        print('Captured %d JSON examples at API %s.' %
              (len(fixture.output), VERSION))
    finally:
        fixture.doCleanups()


if __name__ == '__main__':
    main()
