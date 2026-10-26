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

import re

from alembic import command
from alembic.migration import MigrationContext
from alembic import util
from oslo_config import cfg
from oslo_db import exception as db_exception
from oslo_log import log as logging
from oslo_utils import strutils
from sqlalchemy import exc

from coast.common import config
from coast.db import api as db_api
from coast.db import migration
from coast.i18n import _


CONF = cfg.CONF
LOG = logging.getLogger(__name__)


def add_command_parsers(subparsers):
    parser = subparsers.add_parser('upgrade', help='Upgrade the database.')
    parser.add_argument('revision', nargs='?', default='head')
    subparsers.add_parser('current', help='Show the current DB revision.')
    subparsers.add_parser('version', help='Show the latest schema revision.')
    parser = subparsers.add_parser('revision', help='Create a dev migration.')
    parser.add_argument('-m', '--message')
    parser.add_argument('--autogenerate', action='store_true')


command_opt = cfg.SubCommandOpt('command', handler=add_command_parsers,
                                title='Commands', help='Database commands')


def _database_revision(alembic_config):
    """Return the stamped revision, or None when the database has none."""
    # Same engine as env.py: an injected engine, otherwise oslo.db.
    # https://alembic.sqlalchemy.org/en/latest/api/runtime.html
    engine = alembic_config.attributes.get('engine')
    if engine is None:
        engine = db_api.get_engine()
    with engine.connect() as connection:
        return MigrationContext.configure(connection).get_current_revision()


def _log_database_revision(when, revision):
    """Log one revision at INFO and write the same line to stdout.

    Oslo writes the log file only when log_dir or log_file is set. Stdout
    stays available when that file is not configured.
    """
    shown = 'none' if revision is None else revision
    LOG.info('Database revision %s upgrade: %s', when, shown)
    print('Database revision %s upgrade: %s' % (when, shown), flush=True)


def main():
    CONF.register_cli_opt(command_opt)
    config.parse_args()
    config.setup_logging()
    conf = migration.get_config()
    name = CONF.command.name
    try:
        if name == 'upgrade':
            revision = CONF.command.revision
            if '-' in revision:
                raise SystemExit(_('Downgrades are not supported.'))
            _log_database_revision('before', _database_revision(conf))
            command.upgrade(conf, revision)
            _log_database_revision('after', _database_revision(conf))
        elif name == 'current':
            command.current(conf)
        elif name == 'version':
            command.heads(conf)
        elif name == 'revision':
            command.revision(conf, message=CONF.command.message,
                             autogenerate=CONF.command.autogenerate)
        else:
            raise SystemExit(_('A database command is required.'))
    except (util.CommandError, db_exception.DBError,
            exc.SQLAlchemyError) as error:
        # Keep the traceback, but never log raw exception chains or URLs.
        detail = re.sub(r"[a-zA-Z][a-zA-Z0-9+.-]*://[^\s'\"]+",
                        '<connection URL removed>', str(error))
        safe_error = RuntimeError(strutils.mask_password(detail))
        LOG.exception('Database migration command failed (%s).',
                      type(error).__name__,
                      exc_info=(RuntimeError, safe_error, error.__traceback__))
        raise SystemExit(_('Database migration command failed.'))
    return 0
