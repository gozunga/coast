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

from types import SimpleNamespace
from unittest import mock

from openstack.image.v2 import image as sdk_image

from coast.conductor import controller
from coast.conductor import worker
from coast.tests import base


class ImageSelectionTest(base.TestCase):
    def setUp(self):
        super().setUp()
        self.config.config(image_tag_controller='shared',
                           image_tag_worker='shared', image_owner_id='owner',
                           group='controller_worker')
        self.cloud = controller.ControllerCloud(
            self.conf, SimpleNamespace(project_id='project', id='stack'))
        self.proxy = mock.Mock()
        self.cloud.proxy = lambda service: self.proxy
        self.images = [sdk_image.Image(
            id=role, owner='owner', tags=['shared'], status='active',
            properties={'coast_role': role})
            for role in ('controller', 'worker')]
        self.proxy.images.side_effect = lambda **kw: iter(self.images)

    def test_shared_tag_selects_each_role(self):
        for role in ('controller', 'worker'):
            self.assertEqual(role, self.cloud.select_image(role).id)
        cloud = worker.WorkerCloud(self.conf, self.cloud.stack)
        cloud.proxy = self.cloud.proxy
        self.assertEqual('worker', cloud.select_image('worker').id)

    def test_missing_wrong_role_owner_tag_or_status_fails_closed(self):
        for role in ('controller', 'worker'):
            selected = next(image for image in self.images if image.id == role)
            for attribute, value in (
                    ('properties', {}),
                    ('properties', {'coast_role': 'build'}),
                    ('owner', 'foreign'), ('tags', []), ('tags', None),
                    ('status', 'queued')):
                original = getattr(selected, attribute)
                setattr(selected, attribute, value)
                self.assertRaises(ValueError, self.cloud.select_image, role)
                setattr(selected, attribute, original)

    def test_ambiguity_and_explicit_id(self):
        for role in ('controller', 'worker'):
            duplicate = sdk_image.Image(
                id='other-' + role, owner='owner', tags=['shared'],
                status='active', properties={'coast_role': role})
            self.images.append(duplicate)
            self.assertRaises(ValueError, self.cloud.select_image, role)
            self.config.config(group='controller_worker',
                               **{role + '_image_id': duplicate.id})
            self.assertEqual(duplicate.id, self.cloud.select_image(role).id)
            for identifier in ('missing',
                               'worker' if role == 'controller' else
                               'controller'):
                self.config.config(group='controller_worker',
                                   **{role + '_image_id': identifier})
                self.assertRaises(ValueError, self.cloud.select_image, role)
            self.config.config(group='controller_worker',
                               **{role + '_image_id': duplicate.id})
            duplicate.owner = 'foreign'
            self.assertRaises(ValueError, self.cloud.select_image, role)
            self.images.remove(duplicate)

    def test_tag_and_owner_required_even_with_id(self):
        self.config.config(controller_image_id='controller',
                           group='controller_worker')
        for option in ('image_tag_controller', 'image_owner_id'):
            original = getattr(self.conf.controller_worker, option)
            self.config.config(group='controller_worker', **{option: None})
            self.assertRaises(ValueError, self.cloud.select_image,
                              'controller')
            self.config.config(group='controller_worker', **{option: original})

    def test_customer_id_or_name_overrides_default_id(self):
        self.config.config(controller_image_id='controller',
                           group='controller_worker')
        other = sdk_image.Image(
            id='alternative', name='published', owner='owner', tags=['shared'],
            status='active', properties={'coast_role': 'controller'})
        self.images.append(other)
        for selection in ('alternative', 'published'):
            self.assertEqual(other.id, self.cloud.select_image(
                'controller', selection).id)
        self.assertEqual(
            'controller', self.cloud.select_image('controller').id)

    def test_customer_selection_is_unique_across_ids_and_names(self):
        selected = self.images[0]
        selected.name = 'published'
        duplicate = sdk_image.Image(
            id='published', name='other', owner='owner', tags=['shared'],
            status='active', properties={'coast_role': 'controller'})
        self.images.append(duplicate)
        self.assertRaises(ValueError, self.cloud.select_image,
                          'controller', 'published')
        self.assertRaises(ValueError, self.cloud.select_image,
                          'controller', 'missing')

    def test_customer_selection_preserves_all_catalog_checks(self):
        for attribute, value in (
                ('owner', 'foreign'), ('tags', []), ('status', 'queued'),
                ('properties', {'coast_role': 'worker'})):
            selected = self.images[0]
            original = getattr(selected, attribute)
            setattr(selected, attribute, value)
            self.assertRaises(ValueError, self.cloud.select_image,
                              'controller', selected.id)
            setattr(selected, attribute, original)
        self.assertRaises(ValueError, self.cloud.select_image,
                          'controller', 'worker')
