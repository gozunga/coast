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


"""Keep engine names out of text that project users read.

Customer-facing text never names the app engine or its
proxy. Driver and client errors keep the engine name so operator logs and
admin reads stay useful. Customer reads pass through this boundary.
"""

import re

from oslo_utils import strutils

from coast.common import engine_names


ENGINE_NAME = engine_names.ENGINE_NAME
contains_engine_name = engine_names.contains_engine_name
# JSON can spell a name with \uXXXX escapes; decode such bodies too.
_ESCAPE = re.compile(rb'\\u[0-9a-fA-F]{4}')

# Response keys whose string values Coast writes: faults, errors, warnings,
# reasons, notes and event messages. Other keys hold customer data (names,
# env values, log lines) or engine content (templates) and stay as they are.
TEXT_KEYS = frozenset((
    'error', 'errors', 'fault', 'faultstring', 'message', 'note', 'reason',
    'status_reason', 'unavailable', 'warning', 'warnings'))
# Named diagnostics such as ``reconcile_fault`` or ``failover_warnings``.
TEXT_SUFFIXES = ('_error', '_errors', '_fault', '_message', '_note',
                 '_reason', '_warning', '_warnings')
# Customer data. Keys under these never hold Coast text.
DATA_KEYS = frozenset((
    'env', 'environment', 'labels', 'lines', 'variables'))


def scrub(text):
    """Return text with secrets masked and engine names replaced."""
    if not text or not isinstance(text, str):
        return text
    return engine_names.neutral(strutils.mask_password(text))


def may_need_scrub(body):
    """Cheap check on a raw JSON body before decoding it."""
    return bool(body) and bool(
        ENGINE_NAME.search(body) or _ESCAPE.search(body))


def is_text_key(key):
    return isinstance(key, str) and (
        key in TEXT_KEYS or key.endswith(TEXT_SUFFIXES))


def scrub_tree(value, key=None):
    """Scrub Coast text fields in a decoded JSON body."""
    if isinstance(value, dict):
        return {k: (v if k in DATA_KEYS else scrub_tree(v, k))
                for k, v in value.items()}
    if isinstance(value, list):
        return [scrub_tree(item, key) for item in value]
    if isinstance(value, str) and is_text_key(key):
        return scrub(value)
    return value
