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
import re
import shutil
import stat
import subprocess
import tempfile

from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography import x509
from cryptography.x509.oid import ExtendedKeyUsageOID

from coast.tests import base


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / 'tools' / 'gen-ca.sh'
ISO = re.compile(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$')
LINE = re.compile(r'^(OK|EXPIRING) (\S+) (\S+)$')


class GenCATest(base.TestCase):

    def setUp(self):
        super().setUp()
        if shutil.which('openssl') is None:
            self.skipTest('openssl is not present')
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.output = Path(self.directory.name)

    def _run(self, *args):
        result = subprocess.run(
            [str(SCRIPT), *args],
            check=False, capture_output=True, text=True)
        combined = result.stdout + result.stderr
        self.assertNotIn('PRIVATE KEY', combined)
        self.assertNotIn('BEGIN ', combined)
        return result

    def _generate(self, *extra):
        result = self._run(
            '--output-dir', str(self.output), '--days', '30', *extra)
        self.assertEqual(0, result.returncode, result.stderr)
        return result

    def _mode(self, name):
        return stat.S_IMODE((self.output / name).stat().st_mode)

    def _cert(self, name):
        return x509.load_pem_x509_certificate(
            (self.output / name).read_bytes())

    def _assert_signed_by(self, leaf, issuer):
        public_key = issuer.public_key()
        algorithm = leaf.signature_hash_algorithm
        if isinstance(public_key, rsa.RSAPublicKey):
            public_key.verify(
                leaf.signature, leaf.tbs_certificate_bytes,
                padding.PKCS1v15(), algorithm)
        else:
            public_key.verify(
                leaf.signature, leaf.tbs_certificate_bytes,
                ec.ECDSA(algorithm))
        self.assertEqual(issuer.subject, leaf.issuer)

    def test_writes_cas_client_cert_and_permissions(self):
        self._generate('--registry-host', 'registry.example.com')
        self.assertEqual(
            0o700, stat.S_IMODE(self.output.stat().st_mode))
        self.assertEqual(0o600, self._mode('coast_server_ca.key.pem'))
        self.assertEqual(0o600, self._mode('coast_client_ca.key.pem'))
        self.assertEqual(0o600, self._mode('coast_client.cert-and-key.pem'))
        self.assertEqual(0o600, self._mode('coast_registry.key.pem'))
        self.assertEqual(0o644, self._mode('coast_server_ca.cert.pem'))
        self.assertEqual(0o644, self._mode('coast_client_ca.cert.pem'))
        self.assertEqual(0o644, self._mode('coast_registry.cert.pem'))

        server_ca = self._cert('coast_server_ca.cert.pem')
        client_ca = self._cert('coast_client_ca.cert.pem')
        client = self._cert('coast_client.cert-and-key.pem')
        registry = self._cert('coast_registry.cert.pem')
        self.assertTrue(server_ca.extensions.get_extension_for_class(
            x509.BasicConstraints).value.ca)
        self.assertTrue(client_ca.extensions.get_extension_for_class(
            x509.BasicConstraints).value.ca)
        self._assert_signed_by(client, client_ca)
        self._assert_signed_by(registry, server_ca)
        self.assertNotEqual(client.issuer, server_ca.subject)
        client_eku = client.extensions.get_extension_for_class(
            x509.ExtendedKeyUsage).value
        registry_eku = registry.extensions.get_extension_for_class(
            x509.ExtendedKeyUsage).value
        self.assertIn(ExtendedKeyUsageOID.CLIENT_AUTH, client_eku)
        self.assertIn(ExtendedKeyUsageOID.SERVER_AUTH, registry_eku)
        sans = registry.extensions.get_extension_for_class(
            x509.SubjectAlternativeName).value
        self.assertEqual(
            ['registry.example.com'],
            sans.get_values_for_type(x509.DNSName))

    def test_idempotent_run_keeps_keys_until_force(self):
        self._generate()
        bundle = self.output / 'coast_client.cert-and-key.pem'
        first = bundle.read_bytes()
        again = self._generate()
        self.assertIn('unchanged coast_client.cert-and-key.pem',
                      again.stdout)
        self.assertEqual(first, bundle.read_bytes())
        forced = self._generate('--force')
        self.assertEqual(0, forced.returncode)
        self.assertNotEqual(first, bundle.read_bytes())
        self.assertIn('wrote coast_server_ca.cert.pem', forced.stdout)

    def test_check_expiry_exit_codes_and_lines(self):
        self._generate('--registry-host', 'registry.example.com')
        fresh = self._run(
            '--output-dir', str(self.output), '--check-expiry', '1')
        self.assertEqual(0, fresh.returncode, fresh.stderr)
        soon = self._run(
            '--output-dir', str(self.output), '--check-expiry', '40')
        self.assertEqual(1, soon.returncode, soon.stderr)
        names = set()
        for line in soon.stdout.splitlines():
            match = LINE.match(line)
            self.assertIsNotNone(match, line)
            self.assertEqual('EXPIRING', match.group(1))
            self.assertIsNotNone(ISO.match(match.group(3)))
            names.add(match.group(2))
        self.assertEqual({
            'coast_server_ca.cert.pem',
            'coast_client_ca.cert.pem',
            'coast_client.cert-and-key.pem',
            'coast_registry.cert.pem',
        }, names)
        for line in fresh.stdout.splitlines():
            self.assertTrue(line.startswith('OK '), line)

    def test_check_expiry_does_not_write(self):
        self._generate()
        before = {
            path.name: path.read_bytes()
            for path in self.output.iterdir() if path.is_file()}
        result = self._run(
            '--output-dir', str(self.output),
            '--check-expiry', '1', '--force')
        self.assertEqual(0, result.returncode, result.stderr)
        after = {
            path.name: path.read_bytes()
            for path in self.output.iterdir() if path.is_file()}
        self.assertEqual(before, after)
        missing = self._run(
            '--output-dir', str(self.output / 'missing'),
            '--check-expiry', '1')
        self.assertEqual(2, missing.returncode)
        self.assertFalse((self.output / 'missing').exists())

    def test_usage_errors(self):
        self.assertEqual(2, self._run('--days', '30').returncode)
        self.assertEqual(
            2, self._run('--output-dir', str(self.output),
                         '--days', '0').returncode)
        self.assertEqual(
            2, self._run('--output-dir', str(self.output),
                         '--key-size', '1024').returncode)
        self.assertFalse(any(self.output.iterdir()))

    def test_ec_key_type(self):
        result = self._generate('--key-type', 'ec', '--key-size', '256')
        self.assertEqual(0, result.returncode)
        ca_cert = self._cert('coast_server_ca.cert.pem')
        self.assertIsInstance(ca_cert.public_key(), ec.EllipticCurvePublicKey)
