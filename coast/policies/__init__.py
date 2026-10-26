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

import itertools

from coast.policies import app
from coast.policies import app_environment
from coast.policies import appgroup
from coast.policies import backup
from coast.policies import base
from coast.policies import console
from coast.policies import database
from coast.policies import deployment
from coast.policies import dns
from coast.policies import domain
from coast.policies import environment
from coast.policies import event
from coast.policies import export
from coast.policies import file_mount
from coast.policies import plan
from coast.policies import quota
from coast.policies import secret
from coast.policies import server
from coast.policies import storage
from coast.policies import templates
from coast.policies import volume


def list_rules():
    """Return all policy defaults for registration and sample generation."""
    return itertools.chain(
        base.list_rules(),
        backup.list_rules(),
        database.list_rules(),
        environment.list_rules(),
        export.list_rules(),
        appgroup.list_rules(),
        app_environment.list_rules(),
        server.list_rules(),
        volume.list_rules(),
        storage.list_rules(),
        file_mount.list_rules(),
        app.list_rules(),
        console.list_rules(),
        deployment.list_rules(),
        domain.list_rules(),
        dns.list_rules(),
        secret.list_rules(),
        event.list_rules(),
        quota.list_rules(),
        plan.list_rules(),
        templates.list_rules(),
    )
