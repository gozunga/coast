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

"""Database tools run only in the selected customer service container."""

import contextlib
import re
import subprocess
import threading

from coast.agent import containers


class BackupError(Exception):
    """A safe error without command output, credentials or URLs."""


# Shell expansion occurs inside the DB container. No secret enters argv on
# the worker. Use a socket for SQL and loopback for MongoDB.
SCRIPTS = {
    'postgres': (
        'export PGPASSWORD="${POSTGRES_PASSWORD:-}"; '
        'u="${POSTGRES_USER:-postgres}"; '
        'd="${POSTGRES_DB:-$u}"; ',
        ['pg_dump'], ['pg_restore'],
        'exec "$tool" --format=custom --compress=0 --no-owner '
        '--username="$u" "$d"',
        'exec "$tool" --exit-on-error --clean --if-exists --no-owner '
        '--username="$u" --dbname="$d"'),
    'mysql': (
        'export MYSQL_PWD="${MYSQL_ROOT_PASSWORD:-${MYSQL_PASSWORD:-}}"; '
        'u=root; if [ -z "${MYSQL_ROOT_PASSWORD:-}" ]; then '
        'u="${MYSQL_USER:-root}"; fi; d="${MYSQL_DATABASE:-mysql}"; ',
        ['mysqldump'], ['mysql'],
        'exec "$tool" --single-transaction --user="$u" "$d"',
        'exec "$tool" --user="$u" "$d"'),
    'mariadb': (
        'export MYSQL_PWD="${MARIADB_ROOT_PASSWORD:-'
        '${MYSQL_ROOT_PASSWORD:-${MARIADB_PASSWORD:-${MYSQL_PASSWORD:-}}}}"; '
        'u=root; if [ -z "${MARIADB_ROOT_PASSWORD:-'
        '${MYSQL_ROOT_PASSWORD:-}}" ]; then '
        'u="${MARIADB_USER:-${MYSQL_USER:-root}}"; fi; '
        'd="${MARIADB_DATABASE:-${MYSQL_DATABASE:-mysql}}"; ',
        ['mariadb-dump', 'mysqldump'], ['mariadb', 'mysql'],
        'exec "$tool" --single-transaction --user="$u" "$d"',
        'exec "$tool" --user="$u" "$d"'),
    'mongodb': (
        'set --; if [ -n "${MONGO_INITDB_ROOT_USERNAME:-}" ]; then '
        'set -- --username="$MONGO_INITDB_ROOT_USERNAME" '
        '--password="$MONGO_INITDB_ROOT_PASSWORD" '
        '--authenticationDatabase=admin; fi; ',
        ['mongodump'], ['mongorestore'],
        'exec "$tool" --archive "$@"',
        'exec "$tool" --archive --drop "$@"'),
}


def script(engine, restore=False, version=False, standalone=False):
    prefix, dumps, loads, dump, load = SCRIPTS[engine]
    if standalone:
        if engine == 'mongodb':
            # Do not copy admin users: the new database owns fresh credentials.
            dump = 'exec "$tool" --archive --db="$MONGO_INITDB_DATABASE" "$@"'
            load = ('exec "$tool" --archive --drop '
                    '--nsInclude="$MONGO_INITDB_DATABASE.*" "$@"')
        if engine == 'postgres':
            load = load.replace('--no-owner', '--no-owner --no-acl')
    tools = loads if restore or version else dumps
    detect = 'tool=; for t in ' + ' '.join(tools) + '; do '
    detect += ('if command -v "$t" >/dev/null 2>&1; then tool="$t"; '
               'break; fi; done; [ -n "$tool" ] || exit 127; ')
    if version:
        # Query the server, not the dump utility (MongoDB tools have their
        # own unrelated version numbering).
        if engine == 'postgres':
            return prefix + 'exec psql -At -U "$u" -d "$d" -c ' + (
                "'SHOW server_version'")
        if engine in ('mysql', 'mariadb'):
            return prefix + detect + (
                'exec "$tool" --user="$u" --skip-column-names '
                '-e "SELECT VERSION()"')
        return 'exec mongod --version'
    return prefix + detect + (load if restore else dump)


def major(engine, version):
    match = re.search(r'(\d+)\.(\d+)', version)
    if not match:
        raise BackupError('Database server version could not be determined')
    first, second = map(int, match.groups())
    # PostgreSQL before 10 and MariaDB use two-component release series.
    if engine == 'mariadb' or engine == 'postgres' and first < 10:
        return first, second
    return first, 0


class Docker:
    def __init__(self, reader=None, timeout=3600):
        self.timeout = timeout
        self.reader = reader or containers.Reader()

    def resolve(self, service, container, kind='service'):
        inventory = self.reader.inventory(kind, service)
        name = container if container in inventory else (
            container.removeprefix('c-'))
        if name not in inventory:
            raise BackupError('Database container was not found')
        cid = inventory[name]
        row = self.reader.inspect(cid, kind, service)
        if not row.get('State', {}).get('Running'):
            raise BackupError('Database container is not running')
        return cid

    @contextlib.contextmanager
    def process(self, cid, engine, restore=False, standalone=False):
        args = ['docker', 'exec', '-i', cid, 'sh', '-c',
                script(engine, restore=restore, standalone=standalone)]
        with subprocess.Popen(args, stdin=subprocess.PIPE if restore else
                              subprocess.DEVNULL, stdout=subprocess.DEVNULL
                              if restore else subprocess.PIPE,
                              stderr=subprocess.DEVNULL) as process:
            timer = threading.Timer(self.timeout, process.kill)
            timer.start()
            try:
                yield process.stdin if restore else process.stdout
                if restore:
                    process.stdin.close()
                if process.wait(timeout=60):
                    raise BackupError('Database tool failed; check container '
                                      'credentials and installed tools')
            finally:
                timer.cancel()
                if process.poll() is None:
                    process.kill()
                    process.wait()

    def version(self, cid, engine):
        try:
            result = subprocess.run(
                ['docker', 'exec', cid, 'sh', '-c',
                 script(engine, version=True)], check=False,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30)
            if result.returncode or len(result.stdout) > 4096:
                raise BackupError('Database version query failed')
            value = result.stdout.decode('ascii').strip()
            match = re.search(r'\d+\.\d+(?:\.\d+)?', value)
            if not match:
                raise BackupError('Database server version is unavailable')
            return match.group()
        except (OSError, UnicodeError, subprocess.TimeoutExpired):
            raise BackupError('Database version query failed') from None
