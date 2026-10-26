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

"""Issue Coast leaf certificates from the configured CA.

``tools/gen-ca.sh`` creates the server CA and the client CA. This module
signs one leaf at a time from the CA named by ``[certificates]``. Point
that CA at the server CA for server certificates, or at the client CA for
agent certificates. Issuance does not use a CSR. The private key stays in
memory and must not be logged.
"""

from datetime import datetime
from datetime import timedelta
from datetime import timezone
import ipaddress

from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives import serialization
from cryptography import x509
from cryptography.x509.oid import ExtendedKeyUsageOID
from cryptography.x509.oid import NameOID
from oslo_log import log as logging

from coast.common import config
from coast.common import exception
from coast.i18n import _

LOG = logging.getLogger(__name__)

_DIGESTS = {
    'sha256': hashes.SHA256,
    'sha384': hashes.SHA384,
    'sha512': hashes.SHA512,
}
_SERVER = 'server'
_CLIENT = 'client'
_LEAF_KEY_BITS = 2048


class IssuedCertificate(object):
    """PEM certificate and private key for one leaf.

    ``private_key_pem`` is unencrypted PKCS8. Do not log this object.
    """

    def __init__(self, certificate_pem, private_key_pem, not_valid_after):
        self.certificate_pem = certificate_pem
        self.private_key_pem = private_key_pem
        self.not_valid_after = not_valid_after

    def __repr__(self):
        return 'IssuedCertificate(not_valid_after=%r)' % self.not_valid_after


def issue_server_certificate(server_id, management_ip, conf=None,
                             validity_seconds=None):
    """Issue a serverAuth leaf for one guest.

    The common name is the server id. The SAN is the management IP.
    """
    return issue_certificate(
        server_id, ip_addresses=(management_ip,), usage=_SERVER,
        conf=conf, validity_seconds=validity_seconds)


def issue_agent_certificate(server_id, management_ip, conf=None,
                            validity_seconds=None):
    """Issue a clientAuth leaf for the agent on one guest.

    The common name is the server id. The SAN is the management IP.
    """
    return issue_certificate(
        server_id, ip_addresses=(management_ip,), usage=_CLIENT,
        conf=conf, validity_seconds=validity_seconds)


def issue_certificate(common_name, dns_names=(), ip_addresses=(),
                      usage=_SERVER, validity_seconds=None, conf=None):
    """Issue a leaf from the configured Coast CA.

    ``usage`` is ``server`` (serverAuth) or ``client`` (clientAuth).
    ``validity_seconds`` overrides ``[certificates] cert_validity_time``.
    """
    conf = _conf(conf)
    name = _common_name(common_name)
    names = _alternative_names(dns_names, ip_addresses)
    validity = _validity(conf, validity_seconds)
    ca_cert, ca_key = _load_ca(conf)
    leaf_key = rsa.generate_private_key(
        public_exponent=65537, key_size=_LEAF_KEY_BITS)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    builder = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, name)]))
        .issuer_name(ca_cert.subject)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + timedelta(seconds=validity))
        .add_extension(
            x509.BasicConstraints(ca=False, path_length=None),
            critical=True)
        .add_extension(_key_usage(usage), critical=True)
        .add_extension(
            x509.ExtendedKeyUsage([_extended_usage(usage)]),
            critical=False)
        .add_extension(x509.SubjectAlternativeName(names), critical=False)
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(leaf_key.public_key()),
            critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(
                ca_key.public_key()),
            critical=False)
    )
    cert = builder.sign(private_key=ca_key, algorithm=_digest(conf))
    LOG.info('Issued a %(usage)s certificate for %(name)s',
             {'usage': usage, 'name': name})
    return IssuedCertificate(
        certificate_pem=cert.public_bytes(serialization.Encoding.PEM),
        private_key_pem=leaf_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption()),
        not_valid_after=cert.not_valid_after_utc)


def expires_within(certificate_pem, days, now=None):
    """Return True when the certificate expires within ``days``."""
    if days < 0:
        raise exception.CertificateError(_('Expiry window is invalid.'))
    if isinstance(certificate_pem, str):
        certificate_pem = certificate_pem.encode('ascii')
    try:
        cert = x509.load_pem_x509_certificate(certificate_pem)
    except ValueError:
        raise exception.CertificateError(
            _('Could not read the certificate.')) from None
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return cert.not_valid_after_utc <= now + timedelta(days=days)


def _conf(conf):
    return config.CONF if conf is None else conf


def _common_name(common_name):
    if not isinstance(common_name, str) or not common_name.strip():
        raise exception.CertificateError(_('Common name is required.'))
    if len(common_name) > 64 or any(char in common_name
                                    for char in ('\n', '\r', '\x00')):
        raise exception.CertificateError(_('Common name is invalid.'))
    return common_name


def _alternative_names(dns_names, ip_addresses):
    names = []
    for dns_name in dns_names:
        if (not isinstance(dns_name, str) or not dns_name.strip()
                or any(char.isspace() for char in dns_name)):
            raise exception.CertificateError(
                _('Subject alternative name is invalid.'))
        names.append(x509.DNSName(dns_name))
    for address in ip_addresses:
        try:
            names.append(x509.IPAddress(ipaddress.ip_address(str(address))))
        except ValueError:
            raise exception.CertificateError(
                _('Subject alternative name is invalid.')) from None
    if not names:
        raise exception.CertificateError(
            _('A leaf certificate requires a subject alternative name.'))
    return names


def _validity(conf, validity_seconds):
    if validity_seconds is None:
        validity_seconds = conf.certificates.cert_validity_time
    if validity_seconds < 1:
        raise exception.CertificateError(
            _('Certificate lifetime is invalid.'))
    return validity_seconds


def _digest(conf):
    name = conf.certificates.signing_digest
    try:
        return _DIGESTS[name]()
    except KeyError:
        raise exception.CertificateError(
            _('Signing digest is invalid.')) from None


def _extended_usage(usage):
    if usage == _SERVER:
        return ExtendedKeyUsageOID.SERVER_AUTH
    if usage == _CLIENT:
        return ExtendedKeyUsageOID.CLIENT_AUTH
    raise exception.CertificateError(_('Certificate usage is invalid.'))


def _key_usage(usage):
    encrypt = usage == _SERVER
    return x509.KeyUsage(
        digital_signature=True,
        content_commitment=False,
        key_encipherment=encrypt,
        data_encipherment=False,
        key_agreement=False,
        key_cert_sign=False,
        crl_sign=False,
        encipher_only=False,
        decipher_only=False)


def _load_ca(conf):
    cert_path = conf.certificates.ca_certificate
    key_path = conf.certificates.ca_private_key
    if not cert_path or not key_path:
        raise exception.CertificateError(
            _('Coast CA configuration is incomplete.'))
    cert_pem = _read_file(cert_path)
    key_pem = _read_file(key_path)
    passphrase = conf.certificates.ca_private_key_passphrase
    password = passphrase.encode('utf-8') if passphrase else None
    try:
        ca_cert = x509.load_pem_x509_certificate(cert_pem)
        ca_key = serialization.load_pem_private_key(
            key_pem, password=password)
    except (ValueError, TypeError, UnsupportedAlgorithm):
        LOG.error('Could not load the Coast CA material')
        raise exception.CertificateError(
            _('Could not load the Coast CA material.')) from None
    try:
        basic = ca_cert.extensions.get_extension_for_class(
            x509.BasicConstraints).value
    except x509.ExtensionNotFound:
        basic = None
    if basic is None or not basic.ca:
        raise exception.CertificateError(
            _('Coast CA certificate is not a certificate authority.'))
    cert_public = ca_cert.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo)
    key_public = ca_key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo)
    if cert_public != key_public:
        raise exception.CertificateError(
            _('Coast CA certificate and private key do not match.'))
    return ca_cert, ca_key


def _read_file(path):
    try:
        with open(path, 'rb') as handle:
            return handle.read()
    except OSError:
        LOG.error('Could not read Coast CA file %s', path)
        raise exception.CertificateError(
            _('Could not read the Coast CA file.')) from None
