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

"""Resource status transitions; task states are separate CAS locks."""

from coast.common import exception

ACTIVE = 'ACTIVE'
ADDING_SERVER = 'ADDING_SERVER'
AVAILABLE = 'AVAILABLE'
BUILDING = 'BUILDING'
CANCELLED = 'CANCELLED'
CREATING = 'CREATING'
DEGRADED = 'DEGRADED'
DELETED = 'DELETED'
DELETING = 'DELETING'
DEPLOYING = 'DEPLOYING'
ERROR = 'ERROR'
EXPIRED = 'EXPIRED'
FAILED = 'FAILED'
FAILING_OVER = 'FAILING_OVER'
FAILOVER = 'FAILOVER'
IN_USE = 'IN_USE'
NONE = 'NONE'
PENDING = 'PENDING'
PENDING_DNS = 'PENDING_DNS'
PENDING_VERIFICATION = 'PENDING_VERIFICATION'
PROVISIONING = 'PROVISIONING'
RECONCILING = 'RECONCILING'
ROLLED_BACK = 'ROLLED_BACK'
ROLLING_BACK = 'ROLLING_BACK'
SNAPSHOTTING = 'SNAPSHOTTING'
STARTING = 'STARTING'
STOPPED = 'STOPPED'
SUPERSEDED = 'SUPERSEDED'
UNHEALTHY = 'UNHEALTHY'
UPGRADING = 'UPGRADING'
UPLOADING = 'UPLOADING'
VERIFIED = 'VERIFIED'

TRANSITIONS = {
    'backup': {
        PENDING: frozenset([BUILDING, ERROR, DELETING]),
        BUILDING: frozenset([ACTIVE, ERROR]),
        ACTIVE: frozenset([DELETING]),
        ERROR: frozenset([DELETING]),
        DELETING: frozenset([ERROR, DELETED]),
        DELETED: frozenset(),
    },
    'backup_job': {
        PENDING: frozenset([BUILDING, ERROR]),
        BUILDING: frozenset([ACTIVE, ERROR]),
        ACTIVE: frozenset(),
        ERROR: frozenset(),
    },
    'stack': {
        PENDING: frozenset([BUILDING, ERROR, DELETING]),
        BUILDING: frozenset([ACTIVE, ERROR, DELETING]),
        ACTIVE: frozenset([DEGRADED, UPGRADING, ROLLING_BACK, ERROR,
                           DELETING]),
        DEGRADED: frozenset([ACTIVE, ERROR, DELETING]),
        UPGRADING: frozenset([ACTIVE, ERROR, DELETING]),
        ERROR: frozenset([ROLLING_BACK, DELETING]),
        ROLLING_BACK: frozenset([ACTIVE, ERROR, DELETING]),
        DELETING: frozenset([DELETED, ERROR]),
        DELETED: frozenset(),
    },
    'appgroup': {
        PENDING: frozenset([ACTIVE, ERROR, DELETING]),
        ACTIVE: frozenset([DELETING]),
        ERROR: frozenset([DELETING]),
        DELETING: frozenset([DELETED, ERROR]),
        DELETED: frozenset(),
    },
    # `environment` remains the Q47 alias for a stack. This key is the
    # Coolify environment row.
    'app_environment': {
        PENDING: frozenset([ACTIVE, ERROR, DELETING]),
        ACTIVE: frozenset([DELETING]),
        ERROR: frozenset([DELETING]),
        DELETING: frozenset([DELETED, ERROR]),
        DELETED: frozenset(),
    },
    'server': {
        PENDING: frozenset([BUILDING, ERROR, DELETING]),
        BUILDING: frozenset([ACTIVE, ERROR, DELETING]),
        ACTIVE: frozenset([UNHEALTHY, FAILOVER, ERROR, DELETING]),
        UNHEALTHY: frozenset([ACTIVE, FAILOVER, ERROR, DELETING]),
        FAILOVER: frozenset([ACTIVE, UNHEALTHY, ERROR, DELETING]),
        ERROR: frozenset([FAILOVER, DELETING]),
        DELETING: frozenset([ERROR]),
    },
    'volume': {
        CREATING: frozenset([AVAILABLE, ERROR, DELETING]),
        AVAILABLE: frozenset([IN_USE, SNAPSHOTTING, ERROR, DELETING]),
        IN_USE: frozenset([AVAILABLE, SNAPSHOTTING, ERROR, DELETING]),
        SNAPSHOTTING: frozenset([AVAILABLE, IN_USE, ERROR, DELETING]),
        ERROR: frozenset([DELETING]),
        DELETING: frozenset([ERROR]),
    },
    # STARTING: start/restart sent, waiting for the engine to report
    # running. DEPLOYING: a deployment is in flight. The conductor's
    # periodic poll settles both; neither holds the project lock.
    'service': {
        PENDING: frozenset([ACTIVE, STOPPED, ERROR, DELETING, DEPLOYING,
                            STARTING]),
        ACTIVE: frozenset([STOPPED, DEGRADED, ERROR, DELETING, STARTING,
                           DEPLOYING]),
        STOPPED: frozenset([ACTIVE, ERROR, DELETING, STARTING, DEPLOYING]),
        DEGRADED: frozenset([ACTIVE, STOPPED, ERROR, DELETING, STARTING,
                             DEPLOYING]),
        ERROR: frozenset([ACTIVE, STOPPED, DELETING, STARTING, DEPLOYING]),
        STARTING: frozenset([ACTIVE, STOPPED, ERROR, DELETING, DEPLOYING]),
        DEPLOYING: frozenset([ACTIVE, STOPPED, ERROR, DELETING]),
        DELETING: frozenset([ERROR]),
    },
    'deployment': {
        PENDING: frozenset([BUILDING, DEPLOYING, FAILED, CANCELLED]),
        BUILDING: frozenset([DEPLOYING, FAILED, CANCELLED]),
        DEPLOYING: frozenset([ACTIVE, FAILED, CANCELLED]),
        ACTIVE: frozenset([SUPERSEDED, ROLLED_BACK]),
        SUPERSEDED: frozenset(),
        FAILED: frozenset(),
        CANCELLED: frozenset(),
        ROLLED_BACK: frozenset(),
    },
    'domain': {
        PENDING_VERIFICATION: frozenset([VERIFIED, ERROR]),
        VERIFIED: frozenset([PENDING_VERIFICATION, PENDING_DNS,
                             ACTIVE, ERROR]),
        PENDING_DNS: frozenset([PENDING_VERIFICATION, ACTIVE, ERROR]),
        ACTIVE: frozenset([PENDING_VERIFICATION, PENDING_DNS, ERROR]),
        ERROR: frozenset([PENDING_VERIFICATION]),
    },
    'export': {
        PENDING: frozenset([SNAPSHOTTING, ERROR, DELETING]),
        SNAPSHOTTING: frozenset([UPLOADING, ERROR, DELETING]),
        UPLOADING: frozenset([ACTIVE, ERROR, DELETING]),
        ACTIVE: frozenset([EXPIRED, ERROR, DELETING]),
        EXPIRED: frozenset([DELETING]),
        ERROR: frozenset([DELETING]),
        DELETING: frozenset([ERROR]),
    },
    # Hard-deleted. There is no DELETING status.
    'storage': {
        AVAILABLE: frozenset([IN_USE]),
        IN_USE: frozenset([AVAILABLE]),
    },
}

# Q47 event names stay environment and app. State names use stack and service.
_KINDS = {
    'environment': 'stack',
    'app': 'service',
}


def kind(resource):
    """Return the state name for a stored resource name."""
    return _KINDS.get(resource, resource)


STACK_STATES = frozenset(TRANSITIONS['stack'])
APPGROUP_STATES = frozenset(TRANSITIONS['appgroup'])
APP_ENVIRONMENT_STATES = frozenset(TRANSITIONS['app_environment'])
SERVER_STATES = frozenset(TRANSITIONS['server'])
VOLUME_STATES = frozenset(TRANSITIONS['volume'])
SERVICE_STATES = frozenset(TRANSITIONS['service'])
DEPLOYMENT_STATES = frozenset(TRANSITIONS['deployment'])
DOMAIN_STATES = frozenset(TRANSITIONS['domain'])
EXPORT_STATES = frozenset(TRANSITIONS['export'])
BACKUP_STATES = frozenset(TRANSITIONS['backup'])
BACKUP_JOB_STATES = frozenset(TRANSITIONS['backup_job'])
STORAGE_STATES = frozenset(TRANSITIONS['storage'])

TASK_STATES = frozenset([
    NONE, PROVISIONING, ADDING_SERVER, DEPLOYING, ROLLING_BACK, UPGRADING,
    FAILING_OVER, DELETING, RECONCILING
])


def check_transition(resource, from_state, to_state):
    """Validate an edge, including known same-state no-ops.

    Unknown resources or states also raise InvalidStateTransition (409).
    This helper does not update rows, acquire task locks, or emit events.
    """
    resource = kind(resource)
    transitions = TRANSITIONS.get(resource, {})
    if (from_state in transitions and to_state in transitions and
            (from_state == to_state or to_state in transitions[from_state])):
        return
    raise exception.InvalidStateTransition(
        resource=resource, from_state=from_state, to_state=to_state)
