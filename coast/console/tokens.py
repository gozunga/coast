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

"""Console token digest. The database stores only this hash."""

import hashlib


def digest(token):
    """Return the SHA-256 hex digest of a URL-safe token.

    The token is the lookup key. A slow password hash cannot be queried
    by equality. Callers must not log the token or this digest.
    """
    if (not isinstance(token, str) or not token.isascii() or
            not token or len(token) > 128):
        raise ValueError('token')
    return hashlib.sha256(token.encode('ascii')).hexdigest()
