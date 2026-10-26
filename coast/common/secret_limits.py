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

"""Validate text payload sizes without disclosing their contents."""

from oslo_config import cfg

from coast.common import exception


def validate(value, label, max_bytes=None):
    # Barbican checks base64 request bytes before decoding the payload.
    # Match max_secret_bytes to its max_allowed_secret_in_bytes setting.
    # Each four encoded bytes carry three original UTF-8 bytes; report the
    # effective limit in original text bytes, accounting for base64 padding.
    limit = 3 * (cfg.CONF.secrets.max_secret_bytes // 4)
    if max_bytes is not None:
        limit = min(limit, max_bytes)
    try:
        size = len(value.encode('utf-8'))
    except UnicodeError:
        raise exception.Invalid('%s must be valid UTF-8.' % label) from None
    if size > limit:
        raise exception.Invalid(
            '%s exceeds the limit of %d bytes (UTF-8).' % (label, limit))
