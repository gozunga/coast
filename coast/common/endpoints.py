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

"""Validation for operator-supplied M2 engine endpoints."""

from urllib import parse

from coast.drivers import base


def validate_url(value):
    try:
        url = parse.urlsplit(value)
        valid = (url.scheme == 'https' and url.hostname and
                 url.username is None and url.password is None and
                 not url.query and not url.fragment and
                 not any(c.isspace() or c == ',' for c in value))
        url.port
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise base.DriverError('A credential-free HTTPS URL is required')
    return value
