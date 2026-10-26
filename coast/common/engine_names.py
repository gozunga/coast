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

"""Engine and proxy names that project users never read.

Standard library only. The API boundary (``public_text``) and the
template catalog generator (tools/gen-template-catalog.py) share it.
"""

import re


_NAMES = (
    (re.compile('coolify', re.IGNORECASE), 'application engine'),
    (re.compile('traefik', re.IGNORECASE), 'application routing'),
)
ENGINE_NAME = re.compile(b'coolify|traefik', re.IGNORECASE)


def neutral(text):
    """Return text with engine names and em dashes replaced.

    For display text that holds no secrets, such as template metadata.
    ``public_text.scrub`` also masks secrets.
    """
    if not text or not isinstance(text, str):
        return text
    for pattern, word in _NAMES:
        text = pattern.sub(word, text)
    return text.replace('\u2014', '-')


def contains_engine_name(text):
    if isinstance(text, str):
        text = text.encode('utf-8', 'replace')
    return bool(text) and bool(ENGINE_NAME.search(text))
