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

"""Check API links in a relocated, standalone documentation artifact."""

from html.parser import HTMLParser
from pathlib import Path
import shutil
import sys
import tempfile
from urllib.parse import unquote
from urllib.parse import urlsplit


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.references = []
        self.ids = set()

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if 'id' in attributes:
            self.ids.add(attributes['id'])
        if tag == 'a':
            href = dict(attrs).get('href', '')
            if 'api-ref/' in href:
                self.references.append(href)


def check(output):
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / 'download'
        shutil.copytree(output, root)
        parser = Links()
        parser.feed((root / 'app-sources.html').read_text(encoding='utf-8'))
        if len(parser.references) != 5:
            raise ValueError('Expected five app-source API reference links')
        for href in parser.references:
            link = urlsplit(href)
            target = (root / unquote(link.path)).resolve()
            if (link.scheme or link.netloc or not target.is_relative_to(root)
                    or not target.is_file()):
                raise ValueError('API link escapes the publication: ' + href)
            if link.fragment:
                page = Links()
                page.feed(target.read_text(encoding='utf-8'))
                if unquote(link.fragment) not in page.ids:
                    raise ValueError('API link has no matching fragment ID: '
                                     + href)
        print('Five API links resolve in the standalone publication.')


if __name__ == '__main__':
    check(Path(sys.argv[1]))
