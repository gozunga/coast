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

"""Check fixture provenance, redaction and real response parsing."""

import ipaddress
import json
from pathlib import Path
import re
from unittest import mock

import fixtures

from coast.tests import base
from coast.tests.contract.coolify import recording


class RecordingTest(base.TestCase):
    def test_fixture_secret_scan(self):
        patterns = (r"(?i)bearer\s+\S+", r"\d+\|[A-Za-z0-9]{20,}",
                    r"-----BEGIN [^-]+-----", r"103\.229\.",
                    r"ssh-(?:rsa|ed25519)\s+[A-Za-z0-9+/]{20,}")
        for path in recording.FIXTURES.iterdir():
            if path.is_file():
                text = path.read_text()
                for pattern in patterns:
                    self.assertIsNone(re.search(pattern, text), path.name)
                if path.suffix == ".json":
                    for value in re.findall(
                            r"\b(?:\d{1,3}\.){3}\d{1,3}\b", text):
                        address = ipaddress.ip_address(value)
                        self.assertTrue(any(
                            address in ipaddress.ip_network(network)
                            for network in ("192.0.2.0/24", "198.51.100.0/24",
                                            "203.0.113.0/24")), path.name)

    def test_redaction_preserves_shapes_and_relationships(self):
        recorder = recording.Recorder()
        recorder.bind("live-token", "[redacted]")
        source = {"uuid": "a" * 24,
                  "server_uuid": "a" * 24,
                  "ip": "10.0.0.1", "private_key": "private material",
                  "value": "stack secret", "fqdn": "https://real.test",
                  "message": "Bearer live-token user@real.test",
                  "logs": json.dumps([{"timestamp": "2026-10-10T01:00:00Z",
                                       "output": "secret env=value"}])}
        result = recorder.sanitize(source)
        self.assertEqual(result["uuid"], result["server_uuid"])
        self.assertEqual("192.0.2.10", result["ip"])
        self.assertEqual("[redacted]", result["value"])
        self.assertEqual("[redacted]", result["private_key"])
        self.assertEqual("https://host.example.com", result["fqdn"])
        self.assertEqual("[redacted]", json.loads(result["logs"])[0]["output"])
        self.assertEqual("192.0.2.254", recorder.sanitize("192.0.2.254", "ip"))

    def test_live_provenance_and_pairs(self):
        captured = json.loads((recording.FIXTURES / "live.json").read_text())
        self.assertEqual("4.4.3", captured["engine_version"])
        self.assertRegex(captured["capture_date"], r"^\d{4}-\d{2}-\d{2}$")
        self.assertTrue(captured["pairs"])
        for pair in captured["pairs"]:
            self.assertNotIn("headers", pair["request"])
            self.assertIn(pair["request"]["method"],
                          ("GET", "POST", "PATCH", "DELETE"))
            self.assertIsInstance(pair["response"]["status"], int)

    def test_write_refuses_incomplete_capture(self):
        directory = Path(self.useFixture(fixtures.TempDir()).path)
        recorder = recording.Recorder()
        with mock.patch.object(recording, "FIXTURES", directory):
            self.assertRaises(RuntimeError, recorder.write)
        self.assertEqual([], list(directory.iterdir()))

    def test_write_selects_matching_deployment_and_preserves_text(self):
        directory = Path(self.useFixture(fixtures.TempDir()).path)
        recorder = recording.Recorder()
        responses = [("/version", "4.4.3"),
                     ("/applications/app-uuid", {"uuid": "app-uuid"}),
                     ("/applications/app-uuid/envs", [{"uuid": "env-uuid"}]),
                     ("/applications/app-uuid/logs", {"logs": "line"}),
                     ("/deployments/deployment-uuid", {
                         "deployment_uuid": "deployment-uuid",
                         "status": "finished", "logs": "[]"})]
        for path, body in responses:
            recorder.pairs.append({
                "request": {"method": "GET", "path": path,
                            "query": {}, "body": None},
                "response": {"status": 200, "body": body}})
        for path, body in [
                ("/applications/app-uuid/rollback", {
                    "deployment_uuid": "rollback-uuid"}),
                ("/deploy", {"deployments": [{
                    "deployment_uuid": "deployment-uuid"}]}),
                ("/deploy", {"deployments": [{
                    "deployment_uuid": "cancel-uuid"}]})]:
            recorder.pairs.append({
                "request": {"method": "POST", "path": path,
                            "query": {"uuid": ["app-uuid"]}, "body": None},
                "response": {"status": 200, "body": body}})
        with mock.patch.object(recording, "FIXTURES", directory):
            recorder.write()
        selected = json.loads((directory / "v4.4.3.json").read_text())
        self.assertEqual("4.4.3", selected["version"])
        self.assertEqual("deployment-uuid",
                         selected["deploy"]["deployments"][0][
                             "deployment_uuid"])
