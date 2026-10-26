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

"""In-memory Designate with the public recordset proxy interface."""

from types import SimpleNamespace

from coast.network import dns_credentials


class Proxy:
    def __init__(self):
        self.items = {}
        self.zone_items = [SimpleNamespace(id='zone', name='example.com.',
                                           project_id='project'),
                           SimpleNamespace(id='deep', name='sd.example.com.',
                                           project_id='project'),
                           SimpleNamespace(id='foreign', name='other.com.',
                                           project_id='other')]
        self.additional_headers = {}
        self.counter = 0

    def zones(self):
        return self.zone_items

    def recordsets(self, zone, **query):
        return [r for r in self.items.values() if r.zone_id == zone and
                all(getattr(r, k) == v for k, v in query.items())]

    def create_recordset(self, zone, **attrs):
        self.counter += 1
        record = SimpleNamespace(id=str(self.counter), zone_id=zone, **attrs)
        self.items[record.id] = record
        return record

    def update_recordset(self, record, zone_id=None, **attrs):
        for key, value in attrs.items():
            setattr(record, key, value)
        return record

    def delete_recordset(self, record, zone=None, ignore_missing=True):
        self.items.pop(record.id, None)


class Provider(dns_credentials.CredentialProvider):
    def __init__(self, proxy):
        self.dns = proxy

    def proxy(self, project_id, zone_id=None, trust_id=None):
        return self.dns
