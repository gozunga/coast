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

"""Opt-in live fixture recording, with no raw bodies written to disk."""

import datetime
import ipaddress
import json
from pathlib import Path
import re
from urllib import parse


FIXTURES = Path(__file__).parents[2] / "unit/drivers/coolify/fixtures"


class Recorder:
    def __init__(self):
        self.pairs = []
        self.aliases = {}
        self.numeric_ids = {}

    def bind(self, value, placeholder):
        self.aliases[value] = placeholder

    def hook(self, response, **kwargs):
        request = response.request
        url = parse.urlsplit(request.url)
        try:
            body = response.json()
        except ValueError:
            body = (response.text.strip() if url.path.endswith(
                ("/version", "/health")) else "[redacted]")
        self.pairs.append({
            "request": {"method": request.method,
                        "path": url.path.removeprefix("/api/v1"),
                        "query": parse.parse_qs(url.query),
                        "body": json.loads(request.body)
                        if request.body else None},
            "response": {"status": response.status_code, "body": body},
        })
        # Headers (including Authorization) are deliberately not recorded.

    def sanitize(self, value, key=""):
        if isinstance(value, dict):
            return {k: self.sanitize(v, k) for k, v in value.items()}
        if isinstance(value, list):
            return [self.sanitize(v, key) for v in value]
        if value is None or isinstance(value, bool):
            return value
        if key == "id" or key.endswith("_id"):
            if isinstance(value, int):
                return self.numeric_ids.setdefault(value,
                                                   len(self.numeric_ids) + 1)
            return "reference-id"
        if not isinstance(value, str):
            return value
        if key == "logs":
            try:
                rows = json.loads(value)
            except ValueError:
                return "\n".join(
                    (line.split(" ", 1)[0] + " [redacted log]")
                    if re.match(r"^\d{4}-\d{2}-\d{2}T", line)
                    else "[redacted log]" for line in value.splitlines())
            return json.dumps(self.sanitize(rows))
        if key in ("output", "value", "private_key", "public_key",
                   "email", "description", "validation_logs", "message"):
            return "[redacted]"
        for original, replacement in sorted(
                self.aliases.items(), key=lambda item: -len(item[0])):
            value = value.replace(original, replacement)
        if key == "name" and value != "production" and value not in (
                "coast-env", "coast-app", "coast-server",
                "coast-image-app", "coast-second-server"):
            return "placeholder-name"
        value = re.sub(r"\b[A-Za-z0-9]{24}\b", self._reference, value)
        value = re.sub(
            r"\b[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\b",
            self._reference, value)
        value = re.sub(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", self._ip, value)
        value = re.sub(r"[\w.+-]+@[\w.-]+", "user@example.com", value)
        value = re.sub(r"(?i)bearer\s+\S+|\d+\|[A-Za-z0-9]{20,}",
                       "[redacted]", value)
        value = re.sub(r"-----BEGIN .*?-----.*?-----END .*?-----",
                       "[redacted]", value, flags=re.S)
        value = re.sub(r"(?<![\w])(?:[a-zA-Z0-9-]+\.)+[a-zA-Z]{2,}\b",
                       "host.example.com", value)
        if key in ("commit", "git_commit_sha") and re.fullmatch(
                r"[0-9a-fA-F]{40}", value):
            return "a" * 40
        # Keep only protocol fields needed for response-shape tests.
        safe = {"method", "path", "body", "uuid", "deployment_uuid",
                "resource_uuid",
                "name", "status", "message", "key", "type", "stream",
                "timestamp", "created_at", "updated_at", "deleted_at",
                "commit", "git_commit_sha", "server_role", "proxy_type",
                "fs_path", "mount_path", "ip", "ip_previous", "fqdn",
                "environment_name", "project_uuid", "server_uuid",
                "private_key_uuid", "environment_uuid", "domains",
                "ports_exposes", "git_branch", "build_pack", "user"}
        if key not in safe and key:
            return "[redacted]" if value else ""
        return value

    def _reference(self, match):
        value = match.group()
        if value not in self.aliases:
            self.bind(value, "reference-%d" % (len(self.aliases) + 1))
        return self.aliases[value]

    @staticmethod
    def _ip(match):
        try:
            address = ipaddress.ip_address(match.group())
        except ValueError:
            return "192.0.2.10"
        documentation = ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24")
        if any(address in ipaddress.ip_network(net) for net in documentation):
            return str(address)
        return "192.0.2.10"

    def write(self):
        pairs = self.sanitize(self.pairs)
        unique = {}
        for pair in pairs:
            response = pair["response"]
            body = response["body"]
            # Keep the last poll for each deployment/app state. Other
            # responses retain their full shape, including storage changes.
            if (pair["request"]["path"].startswith("/deployments/") and
                    isinstance(body, dict)):
                body = {"status": body.get("status")}
            identity = json.dumps([pair["request"], response["status"], body],
                                  sort_keys=True)
            unique[identity] = pair
        pairs = list(unique.values())
        fixtures = {}
        selectors = {
            "version": ("GET", "/version"),
            "health": ("GET", "/health"),
            "projects": ("GET", "/projects"),
            "project_created": ("POST", "/projects"),
            "environments": ("GET", "/projects/project-uuid/environments"),
            "servers": ("GET", "/servers"),
            "server": ("GET", "/servers/server-uuid"),
            "validation": ("POST", "/servers/server-uuid/validate"),
            "proxy": ("PATCH", "/servers/server-uuid/proxy"),
            "apps": ("GET", "/applications"),
            "app_created": ("POST", "/applications/public"),
            "app": ("GET", "/applications/app-uuid"),
            "envs": ("GET", "/applications/app-uuid/envs"),
            "storages": ("GET", "/applications/app-uuid/storages"),
            "deploy": ("POST", "/deploy"),
            "deployment": ("GET", "/deployments/deployment-uuid"),
            "rollback": ("POST", "/applications/app-uuid/rollback"),
            "action": ("POST", "/applications/app-uuid/start"),
            "logs": ("GET", "/applications/app-uuid/logs"),
            "deleted": ("DELETE", "/applications/app-uuid"),
        }
        for pair in pairs:
            request, response = pair["request"], pair["response"]
            if not 200 <= response["status"] < 300:
                continue
            for name, selector in selectors.items():
                if (request["method"], request["path"]) != selector:
                    continue
                body = response["body"]
                if name == "deploy" and (
                        request["query"].get("uuid") != ["app-uuid"] or
                        body["deployments"][0]["deployment_uuid"] !=
                        "deployment-uuid"):
                    continue
                if name in ("projects", "apps", "servers"):
                    wanted = {"projects": "coast-env", "apps": "coast-app",
                              "servers": "coast-server"}[name]
                    body = [row for row in body if row["name"] == wanted]
                    if not body:
                        continue
                if name == "envs" and not body:
                    continue
                if name == "deployment" and body["status"] != "finished":
                    if body["status"] == "queued":
                        fixtures["queued"] = body
                    continue
                fixtures[name] = body
        required = {"version", "app", "envs", "deployment", "logs", "rollback"}
        if required - fixtures.keys():
            raise RuntimeError("Live fixture capture is incomplete")
        for filename, data in (("live.json", {
                "engine_version": fixtures["version"],
                "capture_date": datetime.date.today().isoformat(),
                "pairs": pairs}), (
                    "v%s.json" % fixtures["version"], fixtures)):
            (FIXTURES / filename).write_text(json.dumps(data, indent=2) + "\n")
