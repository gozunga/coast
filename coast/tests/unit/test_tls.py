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

import datetime
import ipaddress
from pathlib import Path
import tempfile

from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives import serialization
from cryptography import x509
from cryptography.x509.oid import ExtendedKeyUsageOID

from coast.common import exception
from coast.common import tls
from coast.tests import base


SERVER_ID = '11111111-1111-1111-1111-111111111111'
MGMT_IP = '192.0.2.10'


class TLSTest(base.TestCase):

    def setUp(self):
        super().setUp()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.ca_cert = self._write_ca()

    def _write_ca(self, password=None, ca=True, match_key=True):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(
            x509.NameOID.COMMON_NAME, 'Coast Test CA')])
        now = datetime.datetime.now(datetime.timezone.utc)
        builder = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key((key if match_key else other).public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now)
            .not_valid_after(now + datetime.timedelta(days=30)))
        if ca:
            builder = builder.add_extension(
                x509.BasicConstraints(ca=True, path_length=0), critical=True)
        cert = builder.sign(key if match_key else other, hashes.SHA256())
        root = Path(self.directory.name)
        cert_path = root / 'ca.pem'
        key_path = root / 'ca.key'
        cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        algorithm = serialization.NoEncryption()
        if password:
            algorithm = serialization.BestAvailableEncryption(
                password.encode('utf-8'))
        key_path.write_bytes(key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=algorithm))
        key_path.chmod(0o600)
        self.config.config(
            ca_certificate=str(cert_path),
            ca_private_key=str(key_path),
            ca_private_key_passphrase=password,
            cert_validity_time=3600,
            signing_digest='sha256',
            group='certificates')
        return cert

    def _load(self, issued):
        return x509.load_pem_x509_certificate(issued.certificate_pem)

    def _assert_signed_by(self, leaf, ca_cert):
        ca_cert.public_key().verify(
            leaf.signature, leaf.tbs_certificate_bytes,
            padding.PKCS1v15(), leaf.signature_hash_algorithm)
        self.assertEqual(ca_cert.subject, leaf.issuer)

    def test_server_leaf_chain_san_and_eku(self):
        issued = tls.issue_server_certificate(
            SERVER_ID, MGMT_IP, conf=self.conf)
        leaf = self._load(issued)
        self._assert_signed_by(leaf, self.ca_cert)
        self.assertEqual(SERVER_ID, leaf.subject.get_attributes_for_oid(
            x509.NameOID.COMMON_NAME)[0].value)
        sans = leaf.extensions.get_extension_for_class(
            x509.SubjectAlternativeName).value
        self.assertEqual(
            [ipaddress.ip_address(MGMT_IP)],
            sans.get_values_for_type(x509.IPAddress))
        eku = leaf.extensions.get_extension_for_class(
            x509.ExtendedKeyUsage).value
        self.assertIn(ExtendedKeyUsageOID.SERVER_AUTH, eku)
        self.assertNotIn(ExtendedKeyUsageOID.CLIENT_AUTH, eku)
        usage = leaf.extensions.get_extension_for_class(x509.KeyUsage).value
        self.assertTrue(usage.digital_signature)
        self.assertTrue(usage.key_encipherment)
        self.assertFalse(usage.key_cert_sign)
        basic = leaf.extensions.get_extension_for_class(
            x509.BasicConstraints).value
        self.assertFalse(basic.ca)
        self.assertIn(b'PRIVATE KEY', issued.private_key_pem)
        self.assertNotIn('PRIVATE KEY', repr(issued))

    def test_agent_leaf_is_client_auth(self):
        issued = tls.issue_agent_certificate(
            SERVER_ID, MGMT_IP, conf=self.conf)
        leaf = self._load(issued)
        self._assert_signed_by(leaf, self.ca_cert)
        eku = leaf.extensions.get_extension_for_class(
            x509.ExtendedKeyUsage).value
        self.assertIn(ExtendedKeyUsageOID.CLIENT_AUTH, eku)
        self.assertNotIn(ExtendedKeyUsageOID.SERVER_AUTH, eku)
        usage = leaf.extensions.get_extension_for_class(x509.KeyUsage).value
        self.assertTrue(usage.digital_signature)
        self.assertFalse(usage.key_encipherment)

    def test_lifetime_and_digest_come_from_config(self):
        self.config.config(cert_validity_time=7200, signing_digest='sha384',
                           group='certificates')
        issued = tls.issue_server_certificate(
            SERVER_ID, MGMT_IP, conf=self.conf)
        leaf = self._load(issued)
        lifetime = (leaf.not_valid_after_utc
                    - leaf.not_valid_before_utc).total_seconds()
        self.assertEqual(7200, lifetime)
        self.assertIsInstance(leaf.signature_hash_algorithm, hashes.SHA384)
        self.assertEqual(leaf.not_valid_after_utc, issued.not_valid_after)

    def test_serials_are_random(self):
        first = self._load(tls.issue_server_certificate(
            SERVER_ID, MGMT_IP, conf=self.conf))
        second = self._load(tls.issue_server_certificate(
            SERVER_ID, MGMT_IP, conf=self.conf))
        self.assertNotEqual(first.serial_number, second.serial_number)
        self.assertGreater(first.serial_number, 2 ** 32)
        self.assertGreater(second.serial_number, 2 ** 32)

    def test_expires_within(self):
        issued = tls.issue_server_certificate(
            SERVER_ID, MGMT_IP, conf=self.conf,
            validity_seconds=3600)
        self.assertFalse(tls.expires_within(issued.certificate_pem, 0))
        self.assertTrue(tls.expires_within(issued.certificate_pem, 1))
        now = datetime.datetime.now(datetime.timezone.utc)
        expired = (
            x509.CertificateBuilder()
            .subject_name(self.ca_cert.subject)
            .issuer_name(self.ca_cert.subject)
            .public_key(self.ca_cert.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=2))
            .not_valid_after(now - datetime.timedelta(days=1))
            .sign(rsa.generate_private_key(
                public_exponent=65537, key_size=2048), hashes.SHA256()))
        pem = expired.public_bytes(serialization.Encoding.PEM)
        self.assertTrue(tls.expires_within(pem, 0))
        self.assertRaises(
            exception.CertificateError, tls.expires_within, b'not-a-cert', 1)
        self.assertRaises(
            exception.CertificateError, tls.expires_within,
            issued.certificate_pem, -1)

    def test_passphrase_protected_ca_key(self):
        ca_cert = self._write_ca(password='test-passphrase')
        issued = tls.issue_agent_certificate(
            SERVER_ID, MGMT_IP, conf=self.conf)
        leaf = self._load(issued)
        self.assertEqual(ca_cert.subject, leaf.issuer)

    def test_bad_passphrase_is_rejected(self):
        self._write_ca(password='test-passphrase')
        self.config.config(ca_private_key_passphrase='wrong',
                           group='certificates')
        self.assertRaises(
            exception.CertificateError, tls.issue_server_certificate,
            SERVER_ID, MGMT_IP, self.conf)

    def test_missing_ca_configuration(self):
        self.config.config(ca_certificate=None, ca_private_key=None,
                           group='certificates')
        self.assertRaises(
            exception.CertificateError, tls.issue_server_certificate,
            SERVER_ID, MGMT_IP, self.conf)

    def test_non_ca_and_mismatched_key_are_rejected(self):
        self._write_ca(ca=False)
        self.assertRaises(
            exception.CertificateError, tls.issue_server_certificate,
            SERVER_ID, MGMT_IP, self.conf)
        self._write_ca(match_key=False)
        self.assertRaises(
            exception.CertificateError, tls.issue_server_certificate,
            SERVER_ID, MGMT_IP, self.conf)

    def test_leaf_requires_a_name_and_san(self):
        self.assertRaises(
            exception.CertificateError, tls.issue_certificate,
            '', usage='server', conf=self.conf)
        self.assertRaises(
            exception.CertificateError, tls.issue_certificate,
            SERVER_ID, usage='server', conf=self.conf)
        self.assertRaises(
            exception.CertificateError, tls.issue_certificate,
            SERVER_ID, ip_addresses=('not-an-ip',), conf=self.conf)
        self.assertRaises(
            exception.CertificateError, tls.issue_certificate,
            SERVER_ID, ip_addresses=(MGMT_IP,), usage='both', conf=self.conf)
