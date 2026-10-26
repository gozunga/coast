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

"""Generate catalog, compose and volume assets from pinned Coolify source."""

import argparse
import base64
import binascii
import hashlib
import json
from pathlib import Path
import re
import sys
from urllib import request


ENV_KEY = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')
VARIABLE = re.compile(r'(?<!\$)\$(?:\{([A-Za-z_][A-Za-z0-9_]*)'
                      r'[^}]*\}|([A-Za-z_][A-Za-z0-9_]*))')
HTTP_VARIABLE = re.compile(r'\bSERVICE_(?:URL|FQDN)_[A-Za-z0-9_]+\b')
VERSION = re.compile(r'^[0-9]+\.[0-9]+\.[0-9]+(?:[-.][A-Za-z0-9.-]+)?$')
ROOT = Path(__file__).resolve().parents[1]


def _engine_names():
    root = str(ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)
    from coast.common import engine_names
    return engine_names


def normalize(source):
    """Keep metadata and variable names, never compose or env values.

    Project users read the display fields, so they never
    name the app engine or its proxy and have no em dashes. A logo file
    named after the engine, or with an em dash in its name, is dropped
    rather than renamed: no such file exists under another name.
    Template ids and env names are identifiers and stay as upstream has
    them.
    """
    engine_names = _engine_names()
    catalog = []
    for template_id, template in sorted(source.items()):
        compose = base64.b64decode(template.get('compose', ''),
                                   validate=True).decode('utf-8')
        keys = {left or right for left, right in VARIABLE.findall(compose)}
        envs = template.get('envs', {})
        if not isinstance(envs, (dict, list)):
            raise ValueError('Invalid env names')
        for env in envs:
            key = env.split('=', 1)[0]
            if not ENV_KEY.fullmatch(key):
                raise ValueError('Invalid env name')
            keys.add(key)
        ports = template.get('port') or ''
        ports = sorted({int(port.strip()) for port in str(ports).split(',')
                        if port.strip()})
        # Upstream healthcheck metadata can contain invalid ports.
        # Do not advertise them or guess a replacement.
        ports = [port for port in ports if 1 <= port <= 65535]
        http_port = None
        variables = HTTP_VARIABLE.findall(compose)
        for variable in variables:
            suffix = variable.rsplit('_', 1)[-1]
            if suffix.isdigit():
                http_port = int(suffix)
                break
        else:
            if variables and ports:
                http_port = ports[0]
        logo = template.get('logo') or ''
        if engine_names.contains_engine_name(logo) or '\u2014' in logo:
            logo = ''
        name = engine_names.neutral
        item = {
            'id': template_id,
            'name': name(template.get('name') or template_id),
            'description': name(template.get('slogan') or ''),
            'logo': logo,
            'category': name(template.get('category') or ''),
            'tags': sorted({name(tag) for tag in template.get('tags') or []}),
            'required_env': sorted(keys),
            'ports': ports,
            'http_port': http_port,
        }
        if not all(isinstance(item[field], str) for field in
                   ('id', 'name', 'description', 'logo', 'category')):
            raise ValueError('Invalid metadata')
        catalog.append(item)
    return catalog


def _volume_document(source):
    """Return the named-volume index. Do not store a host path."""
    root = str(ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)
    from coast.common.named_volumes import volume_index
    return json.dumps(volume_index(source), indent=2, sort_keys=True) + '\n'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path, nargs='?')
    parser.add_argument('--check', action='store_true',
                        help='Compare output without writing it')
    parser.add_argument('--check-pinned', action='store_true',
                        help='Fetch the digest-locked source and check output')
    parser.add_argument('--version')
    parser.add_argument('--sha256',
                        help='Expected SHA256 of the pinned source file')
    parser.add_argument('--output-dir', type=Path,
                        default=ROOT / 'coast/drivers/coolify/templates')
    args = parser.parse_args()
    if args.check_pinned:
        lock = json.loads((ROOT / 'tools/template-catalog-source.json')
                          .read_text(encoding='utf-8'))
        args.version, args.sha256 = lock['version'], lock['sha256']
        args.check = True
    elif not args.source or not args.sha256:
        parser.error('Source and SHA256 are required')
    if not args.version or not VERSION.fullmatch(args.version):
        parser.error('Invalid version; use a version without the v prefix')
    try:
        if args.check_pinned:
            with request.urlopen(lock['url'], timeout=60) as response:
                raw = response.read(16 * 1024 * 1024 + 1)
        else:
            raw = args.source.read_bytes()
        if hashlib.sha256(raw).hexdigest() != args.sha256.lower():
            parser.error('Source SHA256 mismatch')
        document = json.loads(raw)
        catalog = normalize(document)
        output = json.dumps(catalog, indent=2, sort_keys=True) + '\n'
        volumes = _volume_document(document)
        compositions = json.dumps(
            {key: value['compose'] for key, value in document.items()
             if value.get('compose')}, indent=2, sort_keys=True) + '\n'
    except (OSError, ValueError, TypeError, AttributeError,
            binascii.Error):
        # Do not echo input or parser exceptions: they can contain secrets.
        parser.error('Cannot read or normalize template source')
    target = args.output_dir / ('catalog-%s.json' % args.version)
    volume_target = args.output_dir / ('volumes-%s.json' % args.version)
    compose_target = args.output_dir / ('compose-%s.json' % args.version)
    if args.check:
        compose_match = (compose_target.exists() and
                         compose_target.read_bytes() ==
                         compositions.encode('utf-8'))
        catalog_match = (target.exists() and
                         target.read_bytes() == output.encode('utf-8'))
        volume_match = (volume_target.exists() and
                        volume_target.read_bytes() ==
                        volumes.encode('utf-8'))
        if not catalog_match or not volume_match or not compose_match:
            parser.error('Catalog is out of date; regenerate from '
                         'pinned source')
    else:
        args.output_dir.mkdir(parents=True, exist_ok=True)
        target.write_text(output, encoding='utf-8')
        volume_target.write_text(volumes, encoding='utf-8')
        compose_target.write_text(compositions, encoding='utf-8')


if __name__ == '__main__':
    main()
