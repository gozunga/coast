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

from pathlib import Path
import tempfile

from oslo_config import cfg
from oslo_config import generator
import oslo_messaging
from oslo_messaging import opts as messaging_opts

from coast.common import config
from coast.common import opts
from coast.tests import base


class ConfigTest(base.TestCase):

    def test_all_groups_are_listed_and_registered(self):
        expected = {'DEFAULT', 'agent', 'api', 'conductor', 'health_manager',
                    'controller_worker', 'console',
                    'certificates', 'quotas', 'plans', 'coolify', 'dns',
                    'export', 'notifications', 'service_auth', 'agent_auth',
                    'secrets', 'backup', 'metrics', 'databases',
                    'nova', 'neutron', 'cinder', 'glance', 'barbican',
                    'designate', 'templates', 'domains', 'webhooks'}
        listed = dict(opts.list_opts())
        self.assertEqual(expected, set(listed))
        for group, options in listed.items():
            self.assertTrue(options)
            self.assertEqual(len(options), len({o.name for o in options}))
            for option in options:
                self.assertTrue(option.help)
                # Adapter option names can use hyphens. The runtime name
                # is the destination.
                if group not in ('service_auth', 'agent_auth'):
                    self.assertIn(option.dest, self.conf if group == 'DEFAULT'
                                  else self.conf[group])
        self.assertIsNone(self.conf.service_auth.auth_type)
        self.assertIsNone(self.conf.service_auth.cafile)
        self.assertIsNone(self.conf.agent_auth.auth_type)

    def test_spec_defaults(self):
        self.assertEqual('0.0.0.0', self.conf.api.bind_host)
        self.assertEqual(9600, self.conf.api.bind_port)
        self.assertEqual('0.0.0.0', self.conf.console.bind_host)
        self.assertEqual(9610, self.conf.console.bind_port)
        self.assertEqual('', self.conf.console.public_endpoint)
        self.assertTrue(self.conf.console.tls)
        self.assertEqual(60, self.conf.console.token_ttl)
        self.assertEqual(900, self.conf.console.idle_timeout)
        self.assertEqual(14400, self.conf.console.max_duration)
        self.assertEqual(1000, self.conf.api.max_limit)
        self.assertIsNone(self.conf.health_manager.bind_ip)
        self.assertEqual('coolify_v4', self.conf.controller_worker.driver)
        self.assertFalse(self.conf.controller_worker.enable_baremetal)
        self.assertEqual(['25', '465', '587'],
                         self.conf.controller_worker.egress_blocked_ports)
        self.assertEqual([], self.conf.controller_worker.control_plane_cidrs)
        self.assertEqual('4.4.7', self.conf.coolify.pinned_version)
        self.assertEqual('sha256', self.conf.certificates.signing_digest)
        self.assertEqual(730 * 24 * 60 * 60,
                         self.conf.certificates.cert_validity_time)
        self.assertEqual('sslip.io', self.conf.domains.default_suffix)
        self.assertFalse(self.conf.domains.skip_verification)
        self.assertFalse(self.conf.dns.use_designate)
        self.assertEqual('swift', self.conf.export.backend)
        self.assertEqual('guest', self.conf.export.upload_via)
        self.assertEqual([1, 2, 0, 1, 10, 20, 200, 50],
                         [self.conf.quotas[name] for name in
                          ('environments', 'servers', 'baremetal_servers',
                           'build_servers', 'apps', 'domains', 'volume_gb',
                           'storage_gb')])
        self.assertTrue(self.conf.oslo_messaging_rabbit.ssl)
        self.assertTrue(self.conf.oslo_messaging_rabbit.rabbit_quorum_queue)
        self.assertTrue(self.conf.oslo_policy.enforce_scope)
        self.assertTrue(self.conf.oslo_policy.enforce_new_defaults)
        self.assertEqual('compute', self.conf.nova.service_type)
        self.assertEqual('network', self.conf.neutron.service_type)
        self.assertEqual('block-storage', self.conf.cinder.service_type)
        self.assertEqual('image', self.conf.glance.service_type)
        self.assertEqual('key-manager', self.conf.barbican.service_type)
        self.assertIsNone(self.conf.nova.region_name)
        self.assertIsNone(self.conf.nova.endpoint_type)
        self.assertFalse(self.conf.nova.insecure)
        self.assertIsNone(self.conf.nova.ca_certificates_file)

    def test_config_file_overrides(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'coast.conf'
            path.write_text('[DEFAULT]\n'
                            'resource_name_prefix = coast-test\n'
                            '[api]\nbind_port = 9700\n'
                            '[controller_worker]\nenable_baremetal = true\n')
            conf = cfg.ConfigOpts()
            self.addCleanup(conf.reset)
            config.parse_args(['--config-file', str(path)], conf=conf,
                              default_config_files=[])
            self.assertEqual('coast-test', conf.resource_name_prefix)
            self.assertEqual(9700, conf.api.bind_port)
            self.assertTrue(conf.controller_worker.enable_baremetal)

    def test_default_suffix_config(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'coast.conf'
            path.write_text('[domains]\ndefault_suffix = apps.example.com\n')
            conf = cfg.ConfigOpts()
            self.addCleanup(conf.reset)
            config.parse_args(['--config-file', str(path)], conf=conf,
                              default_config_files=[])
            self.assertEqual('apps.example.com', conf.domains.default_suffix)

    def test_old_default_service_domain_does_not_supply_default_suffix(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'coast.conf'
            path.write_text('[dns]\n'
                            'default_service_domain = apps.example.com\n')
            conf = cfg.ConfigOpts()
            self.addCleanup(conf.reset)
            config.parse_args(['--config-file', str(path)], conf=conf,
                              default_config_files=[])
            self.assertEqual('sslip.io', conf.domains.default_suffix)
            self.assertNotIn('default_service_domain', conf.dns)

    def test_passphrase_is_secret(self):
        options = {o.name: o for o in dict(opts.list_opts())['certificates']}
        self.assertTrue(options['ca_private_key_passphrase'].secret)

    def test_invalid_export_backend(self):
        self.assertRaises(ValueError, self.config.config,
                          backend='unsupported', group='export')

    def test_export_upload_via_conductor_fails_at_load(self):
        # Conductor relay upload is not implemented (#87). The value must
        # stop config loading, not fail later at export time.
        self.assertRaises(ValueError, self.config.config,
                          upload_via='conductor', group='export')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'coast.conf'
            path.write_text('[export]\nupload_via = conductor\n')
            conf = cfg.ConfigOpts()
            self.addCleanup(conf.reset)
            exc = self.assertRaises(
                cfg.ConfigFileValueError, config.parse_args,
                ['--config-file', str(path)], conf=conf,
                default_config_files=[])
            self.assertIn('upload_via', str(exc))
            self.assertIn('guest', str(exc))

    def test_storage_rpc_transport_after_deprecated_rabbit_option_read(self):
        # uWSGI operators configure this option. Reading it logs a
        # deprecation and mutates the registered option before storage GETs
        # create another transport. Transport construction opens no socket.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'messaging.conf'
            path.write_text('[oslo_messaging_rabbit]\n'
                            'heartbeat_in_pthread = true\n')
            conf = cfg.ConfigOpts()
            config.parse_args([], conf=conf,
                              default_config_files=[str(path)])
            for unused in range(2):
                transport = oslo_messaging.get_rpc_transport(
                    conf, url='rabbit://localhost/')
                self.addCleanup(transport.cleanup)
                self.assertTrue(
                    conf.oslo_messaging_rabbit.heartbeat_in_pthread)

    def test_public_messaging_options_are_copies_with_secure_defaults(self):
        original = dict(messaging_opts.list_opts())
        options = dict(opts.list_messaging_opts())
        self.assertEqual(set(original), set(options))
        rabbit = {o.name: o for o in options['oslo_messaging_rabbit']}
        self.assertTrue(rabbit['ssl'].default)
        self.assertTrue(rabbit['rabbit_quorum_queue'].default)
        untouched = {o.name: o for o in
                     dict(messaging_opts.list_opts())['oslo_messaging_rabbit']}
        expected = {o.name: o for o in original['oslo_messaging_rabbit']}
        for name in ('ssl', 'rabbit_quorum_queue'):
            self.assertEqual(expected[name].default, untouched[name].default)
        sample_notes = {o.name: o for o in
                        options['oslo_messaging_notifications']}
        self.assertEqual(['messagingv2'], list(sample_notes['driver'].default))
        self.assertEqual(0, sample_notes['retry'].default)
        fresh = {o.name: o for o in dict(messaging_opts.list_opts())[
            'oslo_messaging_notifications']}
        self.assertFalse(list(fresh['driver'].default))
        self.assertEqual(-1, fresh['retry'].default)

    def test_generated_rabbit_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'coast.conf.sample'
            generator.main([
                '--config-file', 'etc/coast/coast-config-generator.conf',
                '--output-file', str(output)])
            sample = output.read_text()
            rabbit = sample.split('[oslo_messaging_rabbit]\n', 1)[1]
            rabbit = rabbit.split('\n[', 1)[0]
            self.assertIn('#ssl = true\n', rabbit)
            self.assertIn('#rabbit_quorum_queue = true\n', rabbit)
            notes = sample.split('[oslo_messaging_notifications]\n', 1)[1]
            notes = notes.split('\n[', 1)[0]
            self.assertIn('messagingv2', notes)
            self.assertIn('#retry = 0\n', notes)

    def test_notification_defaults_and_override(self):
        conf = cfg.ConfigOpts()
        self.addCleanup(conf.reset)
        config.parse_args([], conf=conf, default_config_files=[])
        self.assertEqual(['messagingv2'],
                         list(conf.oslo_messaging_notifications.driver))
        self.assertEqual(0, conf.oslo_messaging_notifications.retry)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'coast.conf'
            path.write_text('[oslo_messaging_notifications]\n'
                            'driver = noop\nretry = 3\n')
            overridden = cfg.ConfigOpts()
            self.addCleanup(overridden.reset)
            config.parse_args(['--config-file', str(path)], conf=overridden,
                              default_config_files=[])
            self.assertEqual(['noop'], list(
                overridden.oslo_messaging_notifications.driver))
            self.assertEqual(3, overridden.oslo_messaging_notifications.retry)

    def test_rabbit_defaults_can_be_overridden(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'coast.conf'
            path.write_text('[oslo_messaging_rabbit]\nssl = false\n'
                            'rabbit_quorum_queue = false\n')
            conf = cfg.ConfigOpts()
            self.addCleanup(conf.reset)
            config.parse_args(['--config-file', str(path)], conf=conf,
                              default_config_files=[])
            self.assertFalse(conf.oslo_messaging_rabbit.ssl)
            self.assertFalse(conf.oslo_messaging_rabbit.rabbit_quorum_queue)

    def test_rabbit_transport_accepts_registered_options(self):
        conf = cfg.ConfigOpts()
        self.addCleanup(conf.reset)
        opts.register_opts(conf)
        conf([], default_config_files=[])
        transport = oslo_messaging.get_rpc_transport(
            conf, url='rabbit://u:p@localhost:5672/')
        self.addCleanup(transport.cleanup)
        self.assertTrue(conf.oslo_messaging_rabbit.ssl)
        self.assertTrue(conf.oslo_messaging_rabbit.rabbit_quorum_queue)

    def test_notification_read_before_rpc_transport(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'coast.conf'
            path.write_text('[oslo_messaging_rabbit]\n'
                            'heartbeat_in_pthread = true\n')
            conf = cfg.ConfigOpts()
            self.addCleanup(conf.reset)
            config.parse_args(['--config-file', str(path)], conf=conf,
                              default_config_files=[])
            notification = oslo_messaging.get_notification_transport(
                conf, url='rabbit://u:p@localhost:5672/')
            self.addCleanup(notification.cleanup)
            # Connection setup reads this before API creates its RPC client.
            self.assertTrue(conf.oslo_messaging_rabbit.heartbeat_in_pthread)
            rpc = oslo_messaging.get_rpc_transport(
                conf, url='rabbit://u:p@localhost:5672/')
            self.addCleanup(rpc.cleanup)
