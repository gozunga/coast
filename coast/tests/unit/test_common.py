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

from sqlalchemy import text

from coast.common import context
from coast.common import exception
from coast.tests import base


class ContextTest(base.TestCase):

    def test_identity_round_trip(self):
        ctx = context.RequestContext(user_id='user-id',
                                     project_id='project-id',
                                     roles=['member'], request_id='req-test',
                                     system_scope=None)
        restored = context.RequestContext.from_dict(ctx.to_dict())
        self.assertEqual('user-id', restored.user_id)
        self.assertEqual('project-id', restored.project_id)
        self.assertEqual(['member'], restored.roles)
        self.assertEqual('req-test', restored.request_id)
        self.assertFalse(restored.is_admin)
        self.assertIsNone(restored.system_scope)

    def test_system_scope_round_trip(self):
        ctx = context.RequestContext(system_scope='all', roles=['admin'])
        restored = context.RequestContext.from_dict(ctx.to_dict())
        self.assertEqual('all', restored.system_scope)


class ExceptionTest(base.TestCase):

    def test_exception_family(self):
        for cls, code in [(exception.CoastException, 500),
                          (exception.NotFound, 404), (exception.Conflict, 409),
                          (exception.Invalid, 400), (exception.OverQuota, 403),
                          (exception.NotAuthorized, 403)]:
            error = cls()
            self.assertIsInstance(error, exception.CoastException)
            self.assertEqual(code, error.code)
            self.assertEqual(cls.msg_fmt, str(error))

    def test_format_and_explicit_message(self):
        class NamedError(exception.CoastException):
            msg_fmt = 'Resource %(name)s not found.'
        self.assertEqual('Resource service not found.',
                         str(NamedError(name='service')))
        self.assertEqual('Explicit message',
                         str(NamedError('Explicit message')))

    def test_format_failure_does_not_expose_arguments(self):
        class NamedError(exception.CoastException):
            msg_fmt = 'Resource %(name)s not found.'
        error = NamedError(password='must-not-leak')
        self.assertEqual(NamedError.msg_fmt, str(error))
        self.assertNotIn('must-not-leak', str(error))


class DatabaseFixtureTest(base.TestCase):

    def test_enginefacade_transactions(self):
        ctx = context.RequestContext()
        with self.db.facade.writer.using(ctx):
            ctx.session.execute(text('CREATE TABLE sample (id INTEGER)'))
            ctx.session.execute(text('INSERT INTO sample VALUES (1)'))
        with self.db.facade.reader.using(ctx):
            self.assertEqual(1, ctx.session.execute(
                text('SELECT id FROM sample')).scalar_one())
        with self.assertRaises(RuntimeError):
            with self.db.facade.writer.using(ctx):
                ctx.session.execute(text('INSERT INTO sample VALUES (2)'))
                raise RuntimeError('rollback')
        with self.db.facade.reader.using(ctx):
            self.assertEqual(1, ctx.session.execute(
                text('SELECT COUNT(*) FROM sample')).scalar_one())

    def test_independent_engines(self):
        other = self.useFixture(base.DatabaseFixture())
        self.assertIsNot(self.db.engine, other.engine)
        with self.db.engine.begin() as connection:
            connection.execute(text('CREATE TABLE isolated (id INTEGER)'))
        with other.engine.connect() as connection:
            self.assertEqual(0, connection.execute(text(
                "SELECT COUNT(*) FROM sqlite_master WHERE name='isolated'"
            )).scalar_one())
