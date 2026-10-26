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

"""Image PATCH validation and tenant isolation."""

from coast.api import microversions
from coast import objects
from coast.tests import app_api


class ImageUpdateTest(app_api.AppAPITest):
    def setUp(self):
        super().setUp()
        self.headers['OpenStack-API-Version'] = (
            'app-hosting ' + microversions.IMAGE_UPDATE_VERSION)
        self.source.type = 'image'
        self.source.image = 'nginx:1.27'
        self.source.save()

    def request(self, *args, **kwargs):
        headers = kwargs.get('headers') or self.headers
        kwargs['version'] = headers.get(
            'OpenStack-API-Version',
            'app-hosting ' + microversions.IMAGE_UPDATE_VERSION).split()[-1]
        return super().request(*args, **kwargs)

    def patch_source(self, source, status=200, headers=None):
        return self.request('patch', self.path,
                            {'service': {'source': source}},
                            status=status, headers=headers)

    def test_partial_and_combined_updates(self):
        for incoming, expected in (
                ({'image': 'registry.example.com:5000/web'},
                 'registry.example.com:5000/web:1.27'),
                ({'tag': '1.29-alpine'},
                 'registry.example.com:5000/web:1.29-alpine'),
                ({'image': 'nginx', 'tag': 'stable'}, 'nginx:stable')):
            self.patch_source(incoming)
            source = objects.Source.get_by_id(self.ctx, self.source.id)
            self.assertEqual(expected, source.image)
            self.assertEqual(self.source.id, objects.Service.get_by_id(
                self.ctx, self.service.id).source_id)

    def test_bad_references_do_not_change_source(self):
        for image in ('', 'https://example.com/web', 'nginx:latest',
                      'nginx@sha256:abc', 'UPPER', '/host/path',
                      'user:password@example.com/web', 'nginx\n'):
            self.patch_source({'image': image}, status=400)
        for tag in ('', '-bad', 'a/b', 'v1\n', 'x' * 129):
            self.patch_source({'tag': tag}, status=400)
        self.assertEqual('nginx:1.27', objects.Source.get_by_id(
            self.ctx, self.source.id).image)

    def test_old_version_and_wrong_kind(self):
        self.patch_source({'image': 'busybox'}, status=400, headers=dict(
            self.headers, **{'OpenStack-API-Version': 'app-hosting 1.13'}))
        for kind, pack in (('git', 'nixpacks'), ('git', 'compose'),
                           ('template', None)):
            self.source.type = kind
            self.source.build_pack = pack
            self.source.save()
            response = self.patch_source({'image': 'busybox'}, status=400)
            self.assertIn('image source', response.text)
            self.patch_source({'tag': 'latest'}, status=400)

    def test_policy_and_project(self):
        self.patch_source({'image': 'busybox'}, status=401, headers={})
        for change, status in (({'X-Roles': 'reader'}, 403),
                               ({'X-Project-Id': 'other'}, 404)):
            self.patch_source({'image': 'busybox'}, status=status,
                              headers=dict(self.headers, **change))
        self.assertEqual('nginx:1.27', objects.Source.get_by_id(
            self.ctx, self.source.id).image)
