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

"""Detect scattered database image and version declarations in Python."""

import ast
import re

from coast.common import engine_versions


IMAGES = {image for info in engine_versions.DATABASES.values()
          for image in info['versions'].values()}
REPOSITORIES = {image.rsplit(':', 1)[0].rsplit('/', 1)[-1] for image in IMAGES}
IMAGE = re.compile(
    r'(?<![\w.-])(?:[\w.-]+(?::[0-9]+)?/)*(?:' +
    '|'.join(re.escape(repo) for repo in sorted(REPOSITORIES)) +
    r')(?::[\w][\w.-]*|@sha256:[a-f0-9]+)')
VERSION = re.compile(r'v?[0-9]+(?:\.[0-9]+)*(?:[-\w.]*)?\Z')
NAMES = {'database', *engine_versions.DATABASES, *REPOSITORIES}


def violations(source, microversions=False):
    """Return locations, including declarations absent from the catalog.

    Repository matching deliberately ignores tags and registry prefixes.
    Named version assignments and engine/version dictionaries also catch
    versions that have never been approved. API microversions are separate.
    """
    tree = ast.parse(source)
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if IMAGE.search(node.value):
                found.add(node.lineno)
        if microversions:
            continue
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = (node.targets if isinstance(node, ast.Assign)
                       else [node.target])
            names = [target.id.lower() for target in targets
                     if isinstance(target, ast.Name)]
            declaration = any('version' in name and any(
                engine in name for engine in NAMES) for name in names)
            value = node.value
            image_declaration = any('image' in name and any(
                engine in name for engine in NAMES) for name in names)
            if (image_declaration and isinstance(value, ast.Constant) and
                    isinstance(value.value, str) and
                    (':' in value.value or '@' in value.value)):
                found.add(node.lineno)
            if declaration and value is not None and any(
                    isinstance(item, ast.Constant) and
                    VERSION.fullmatch(str(item.value))
                    for item in ast.walk(value)):
                found.add(node.lineno)
        if isinstance(node, ast.Dict):
            values = {key.value: value for key, value in zip(
                node.keys, node.values) if isinstance(key, ast.Constant)}
            engine = values.get('engine')
            if (isinstance(engine, ast.Constant) and
                    engine.value in engine_versions.DATABASES):
                for key in ('version', 'default_version', 'versions'):
                    value = values.get(key)
                    if value is not None and any(
                            isinstance(item, ast.Constant) and
                            VERSION.fullmatch(str(item.value))
                            for item in ast.walk(value)):
                        found.add(value.lineno)
    return sorted(found)
