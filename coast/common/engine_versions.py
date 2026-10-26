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

"""Reviewed stock engine releases. This module uses only the stdlib.

Add a release by adding one row to RELEASES; the last row is the default.
Image digests are operator-reviewed build inputs, never inferred from tags.
"""

# Offline files shared by these reviewed stock releases.
ARTIFACTS = {
    'docker-compose.yml':
        '2532ee9258e10ad4e86cb3c2b46c29fe9329f5a0dace0f7da410744feec279bd',
    'docker-compose.prod.yml':
        '4cd00e83e117e847e6d6bdfe64f2a2f958bb09965692c5230fa9231a0315f80f',
    '.env.production':
        '7895e54f7c9d40063e76afec9b35839048d91e5c3b5107d64ea49ef6987c304a',
    'upgrade.sh':
        '8dc02adee5ea0f27027405243a15e9be8bced5ea290870de90c8ec0ce4979fda',
    'upgrade-postgres.sh':
        'aa65c277fc13ab5b27c349fbfef12defc59c084b244e4b258329e1f6c570ae8d',
}

# Releases sharing the same reviewed install/support contract use this profile.
STOCK = ('add4e64e82b5fcb751415a681c26db2bff939d4f034d024c632ba0b7dc03c090',
         '1.0.17', ('postgres:15-alpine', 'redis:7-alpine',
                    'busybox:1.37', 'registry:3'), ARTIFACTS)
RELEASES = {
    '4.4.3': (
        *STOCK,
        '188b820a31e17cb1f29d692f7b1468b6637025e3d1290cc80181a006aad30fb4'),
    '4.4.7': (
        *STOCK,
        'a087464562e9ee50501eed24abea2af4b40ba27bf44f04413f52a4cad414a4fd'),
}
# The template source is byte-identical for these reviewed releases.
TEMPLATE_VERSION = next(iter(RELEASES))
SUPPORTED_ENGINE_VERSIONS = tuple(RELEASES)
DEFAULT = SUPPORTED_ENGINE_VERSIONS[-1]


def images(version):
    _, helper, support, _, _ = RELEASES[version]
    return {'coollabsio/coolify:' + version,
            'coollabsio/coolify-helper:' + helper, *support}


def build_version():
    # Baked into the immutable root filesystem by diskimage-builder.
    # Source-tree tests and tools use DEFAULT.
    from pathlib import Path
    path = Path('/usr/lib/coast/engine-version')
    version = path.read_text().strip() if path.exists() else DEFAULT
    if version not in RELEASES:
        raise ValueError('Unsupported baked engine version')
    return version


def manifest(version):
    _, _, _, artifacts, versions_sha = RELEASES[version]
    entries = dict(artifacts, **{'versions.json': versions_sha})
    return ''.join(sha + '  ' + name + '\n' for name, sha in entries.items())


# Reviewed standalone database images. Live pull/start checks are performed
# by the deployment lane. Credential mappings are public API field names.
DATABASES = {
    "postgresql": {
        "display_name": "PostgreSQL",
        "backup_engine": "postgres",
        "versions": {
            "17": "postgres:17-alpine",
            "16": "postgres:16-alpine"
        },
        "default_version": "17",
        "port": 5432,
        "backups": True,
        "credentials": {
            "username": "postgres_user",
            "password": "postgres_password",
            "database": "postgres_db"
        }
    },
    "mysql": {
        "display_name": "MySQL",
        "versions": {
            "8.4": "mysql:8.4"
        },
        "default_version": "8.4",
        "port": 3306,
        "backups": True,
        "credentials": {
            "username": "mysql_user",
            "password": "mysql_password",
            "database": "mysql_database",
            "root_password": "mysql_root_password"
        }
    },
    "mariadb": {
        "display_name": "MariaDB",
        "versions": {
            "11.4": "mariadb:11.4"
        },
        "default_version": "11.4",
        "port": 3306,
        "backups": True,
        "credentials": {
            "username": "mariadb_user",
            "password": "mariadb_password",
            "database": "mariadb_database",
            "root_password": "mariadb_root_password"
        }
    },
    "mongodb": {
        "display_name": "MongoDB",
        "versions": {
            "8.0": "mongo:8.0",
            "7.0": "mongo:7.0"
        },
        "default_version": "8.0",
        "port": 27017,
        "backups": True,
        "credentials": {
            "username": "mongo_initdb_root_username",
            "password": "mongo_initdb_root_password",
            "database": "mongo_initdb_database"
        },
        "url_query": "authSource=admin"
    },
    "redis": {
        "display_name": "Redis",
        "versions": {
            "7.4": "redis:7.4-alpine"
        },
        "default_version": "7.4",
        "port": 6379,
        "backups": False,
        "credentials": {
            "password": "redis_password"
        }
    },
    "keydb": {
        "display_name": "KeyDB",
        "versions": {
            "6.3": "eqalpha/keydb:6.3.4"
        },
        "default_version": "6.3",
        "port": 6379,
        "backups": False,
        "credentials": {
            "password": "keydb_password"
        },
        "url_scheme": "redis"
    },
    "dragonfly": {
        "display_name": "Dragonfly",
        "versions": {
            "1.34": "docker.dragonflydb.io/dragonflydb/dragonfly:v1.34.2"
        },
        "default_version": "1.34",
        "port": 6379,
        "backups": False,
        "credentials": {
            "password": "dragonfly_password"
        },
        "url_scheme": "redis"
    },
    "clickhouse": {
        "display_name": "ClickHouse",
        "versions": {
            "25.8": "clickhouse/clickhouse-server:25.8"
        },
        "default_version": "25.8",
        "port": 9000,
        "backups": False,
        "credentials": {
            "username": "clickhouse_admin_user",
            "password": "clickhouse_admin_password"
        }
    }
}
for _database in DATABASES.values():
    _database['has_username'] = 'username' in _database['credentials']
    _database['has_database'] = 'database' in _database['credentials']
