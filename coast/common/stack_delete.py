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

"""Stack deletion preconditions shared by API and conductor."""

from coast.common import exception
from coast.common import public_text
from coast.i18n import _
from coast import objects


def public_fault(reason):
    """Keep useful cleanup diagnostics without exposing engine identities."""
    return public_text.scrub(reason)


def require_empty(ctx, stack):
    names = []
    marker = None
    while True:
        batch = objects.AppgroupList.get_all(
            ctx, filters={'stack_id': stack.id}, sort_key='id', marker=marker)
        if not batch:
            break
        names.extend(app.name for app in batch)
        marker = batch[-1].id
    if names:
        raise exception.Conflict(_(
            '%(stack)s still has %(count)d app groups: %(names)s. '
            'Delete them first.') % {
                'stack': stack.name, 'count': len(names),
                'names': ', '.join(sorted(names))})
