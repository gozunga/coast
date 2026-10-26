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

"""Secret-free comparisons of pinned public API observations."""

import json
from urllib import parse


def env_drift(rows, variables, template):
    if not isinstance(rows, list) or any(
            not isinstance(r, dict) or not isinstance(r.get('key'), str)
            for r in rows):
        return None
    if template and any(item.build_time for item in variables.values()):
        return None
    # Engine defaults (including buildpack controls and their preview copies)
    # are not customer intent. Only Coast-managed rows participate in drift.
    rows = [row for row in rows if row.get('comment') == 'coast-managed']
    if any(row.get('comment') == 'coast-managed' and
           (row.get('is_preview') is True or
            (not template and row.get('is_runtime') is False))
           for row in rows):
        return True
    by_key = {row['key']: row for row in rows}
    if len(by_key) != len(rows):
        return None
    if any(row.get('comment') == 'coast-managed' and key not in variables
           for key, row in by_key.items()):
        return True
    unknown = False
    for key, item in variables.items():
        row = by_key.get(key)
        if row is None or row.get('comment') != 'coast-managed':
            return True
        desired = {'value': item.value, 'is_literal': True,
                   'is_multiline': '\n' in item.value,
                   'is_shown_once': item.is_secret}
        if not template:
            desired.update(is_preview=False, is_buildtime=item.build_time,
                           is_runtime=True)
        for field, value in desired.items():
            if (field not in row or row[field] is None or
                    (field == 'value' and row.get('is_shown_once') is True)):
                unknown = True
            elif row[field] != value:
                return True
    return None if unknown else False


def compose_targets(row, fqdns):
    """Select only known, unambiguous compose domain targets."""
    if 'docker_compose_domains' not in row:
        raise ValueError
    domains = row['docker_compose_domains']
    if isinstance(domains, str):
        domains = json.loads(domains)
    # The pinned API uses null or [] before any target has been assigned.
    if domains is None or domains == []:
        domains = {}
    if not isinstance(domains, dict) or any(
            not isinstance(name, str) or not name or
            not isinstance(item, dict) or 'domain' not in item or
            (item['domain'] is not None and
             not isinstance(item['domain'], str))
            for name, item in domains.items()):
        raise ValueError
    targets = [{'name': name, 'fqdn': item['domain']}
               for name, item in domains.items()]
    if fqdns:
        targets = [item for item in targets if item['fqdn']] or targets
        if len(targets) != 1:
            raise ValueError
    return targets


def template_domain_url(value):
    """Remove the template's internal routing port from an engine URL."""
    parsed = parse.urlsplit(value)
    return parse.urlunsplit(parsed._replace(
        netloc=parsed.netloc.rsplit(':', 1)[0]
        if ':' in parsed.netloc else parsed.netloc))


def domain_drift(row, fqdns, template):
    if not isinstance(row, dict):
        return None
    if not template and row.get('build_pack') == 'dockercompose':
        try:
            targets = compose_targets(row, fqdns)
        except ValueError:
            return None
    elif template:
        apps = row.get('applications')
        if not isinstance(apps, list) or any(
                not isinstance(app, dict) or not app.get('name')
                for app in apps):
            return None
        targets = [app for app in apps if app.get('fqdn')] or apps
        if fqdns and len(targets) != 1:
            return None
    else:
        targets = [row]
    expected = set()
    for name in fqdns:
        text = str(name).strip().lower()
        if not text.startswith(('http://', 'https://')):
            text = 'https://' + text
        expected.add(template_domain_url(text) if template else text)
    unknown = False
    for target in targets:
        if 'fqdn' not in target:
            unknown = True
            continue
        value = target['fqdn']
        if template and isinstance(value, str):
            value = ','.join(template_domain_url(url.strip())
                             for url in value.split(','))
        if value is not None and not isinstance(value, str):
            unknown = True
        elif {url.strip().lower() for url in (value or '').split(',')
              if url.strip()} != expected:
            return True
    return None if unknown else False


def compare(rows, row, variables, fqdns, template):
    return {'env': env_drift(rows, variables, template),
            'domains': domain_drift(row, fqdns, template)}
