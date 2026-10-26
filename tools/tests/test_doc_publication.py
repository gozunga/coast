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

"""Regression tests for independently distributed documentation."""

import importlib.util
from pathlib import Path
import tempfile
import unittest


SPEC = importlib.util.spec_from_file_location(
    'publication', Path(__file__).resolve().parents[1] /
    'check_doc_publication.py')
publication = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(publication)


class PublicationTest(unittest.TestCase):
    def test_artifact_links_survive_relocation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'api-ref').mkdir()
            (root / 'api-ref/services.html').write_text(
                '<section id="source">API reference</section>')
            (root / 'app-sources.html').write_text(
                '<a href="api-ref/services.html#source">API</a>' * 5)
            publication.check(root)

    def test_checkout_links_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'app-sources.html').write_text(
                '<a href="../../../api-ref/build/html/services.html">'
                'API</a>' * 5)
            with self.assertRaisesRegex(ValueError, 'escapes the publication'):
                publication.check(root)

    def test_missing_fragment_in_existing_page_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'api-ref').mkdir()
            (root / 'api-ref/services.html').write_text(
                '<section id="other">API reference</section>')
            (root / 'app-sources.html').write_text(
                '<a href="api-ref/services.html#missing">API</a>' * 5)
            with self.assertRaisesRegex(ValueError, 'no matching fragment ID'):
                publication.check(root)

    def test_percent_encoded_fragment_and_unfragmented_page(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'api-ref').mkdir()
            (root / 'api-ref/services.html').write_text(
                '<section id="source-name">API reference</section>')
            (root / 'app-sources.html').write_text(
                '<a href="api-ref/services.html#source%2Dname">API</a>' * 4
                + '<a href="api-ref/services.html">API</a>')
            publication.check(root)
