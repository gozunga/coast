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

"""Q116: fixed-path durable leaf replacement; immutable CA trust."""

from datetime import datetime
from datetime import timezone
import fcntl
import ipaddress
import os
from pathlib import Path
import stat
import tempfile
import threading

from cryptography.exceptions import InvalidSignature
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography import x509
from cryptography.x509.oid import ExtendedKeyUsageOID
from cryptography.x509.oid import NameOID

from coast.agent.engines import coolify


def validate(cert, key, ca, server_id, address):
    now = datetime.now(timezone.utc)
    cert.verify_directly_issued_by(ca)
    if (not ca.extensions.get_extension_for_class(
            x509.BasicConstraints).value.ca or
            not ca.not_valid_before_utc <= now < ca.not_valid_after_utc or
            not cert.not_valid_before_utc <= now < cert.not_valid_after_utc or
            cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME) != [
                x509.NameAttribute(NameOID.COMMON_NAME, server_id)] or
            cert.extensions.get_extension_for_class(
                x509.SubjectAlternativeName).value.get_values_for_type(
                    x509.IPAddress) != [ipaddress.ip_address(address)] or
            cert.extensions.get_extension_for_class(
                x509.BasicConstraints).value.ca or
            ExtendedKeyUsageOID.SERVER_AUTH not in
            cert.extensions.get_extension_for_class(
                x509.ExtendedKeyUsage).value):
        raise ValueError('Invalid server identity')
    if (key is not None and
            public(key.public_key()) != public(cert.public_key())):
        raise ValueError('Invalid server key')


def public(key):
    return key.public_bytes(serialization.Encoding.DER,
                            serialization.PublicFormat.SubjectPublicKeyInfo)


def read(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or
                info.st_nlink != 1):
            raise ValueError('Invalid certificate file')
        return stream.read(65537)


def write(path, content):
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.certificate-')
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        coolify._sync_directory(path.parent)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class MissingServerCA(ValueError):
    """The immutable server trust configuration is absent."""


class Rotation:
    def __init__(self, conf):
        self.conf = conf
        self.root = Path(conf.agent.state_path) / 'certificates'
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.root.lstat()
        if (self.root.resolve() != self.root or
                not stat.S_ISDIR(info.st_mode) or
                info.st_uid != os.geteuid() or
                stat.S_IMODE(info.st_mode) != 0o700):
            raise ValueError('Certificate state must be owned and private')
        coolify._sync_directory(self.root.parent)
        self.pending = self.root / 'pending'
        self.lock = threading.Lock()
        if os.path.lexists(self.pending):
            raise ValueError('Certificate install needs operator recovery')

    def rotate(self, body, listener, context_factory):
        if (not isinstance(body, dict) or set(body) != {
                'expected_serial', 'certificate', 'private_key'} or
                not all(isinstance(value, str) and value
                        for value in body.values()) or
                not body['expected_serial'].isdecimal()):
            raise ValueError('Invalid certificate request')
        with self.lock:
            fd = os.open(self.root / '.lock', os.O_CREAT | os.O_RDWR |
                         os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'w') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                if os.path.lexists(self.pending):
                    raise coolify.PreconditionFailed()
                if not self.conf.agent.server_ca:
                    raise MissingServerCA('Configure [agent] server_ca before '
                                          'certificate rotation.')
                try:
                    cert_pem = body['certificate'].encode('ascii')
                    key_pem = body['private_key'].encode('ascii')
                    cert = x509.load_pem_x509_certificate(cert_pem)
                    if (cert.public_bytes(serialization.Encoding.PEM) !=
                            cert_pem):
                        raise ValueError('Only one leaf is allowed')
                    key = serialization.load_pem_private_key(key_pem, None)
                    ca = x509.load_pem_x509_certificate(
                        read(self.conf.agent.server_ca))
                    validate(cert, key, ca, self.conf.agent.server_id,
                             self.conf.agent.bind_ip)
                    cert_path = Path(self.conf.agent.server_cert)
                    key_path = Path(self.conf.agent.server_key)
                    old_cert = read(cert_path)
                    old_key = read(key_path)
                    current = x509.load_pem_x509_certificate(old_cert)
                except (ValueError, TypeError, InvalidSignature,
                        UnsupportedAlgorithm, x509.ExtensionNotFound):
                    raise ValueError('Invalid replacement certificate') \
                        from None
                serial = str(cert.serial_number)
                # Different material with the same serial is not a retry.
                if old_cert == cert_pem and old_key == key_pem:
                    return {'status': 'rotated', 'serial': serial}
                if (body['expected_serial'] != str(current.serial_number) or
                        current.serial_number == cert.serial_number):
                    raise coolify.Conflict()
                try:
                    # Save the old pair before recording intent to change it.
                    write(self.root / 'previous.crt', old_cert)
                    write(self.root / 'previous.key', old_key)
                    coolify._claim(self.pending)
                    write(key_path, key_pem)
                    write(cert_path, cert_pem)
                    context = context_factory(self.conf)
                    self.pending.unlink()
                    coolify._sync_directory(self.root)
                    listener.context = context
                except Exception:
                    # Existing requests can finish, but no later TLS connection
                    # can use an uncertain pair. Startup checks the marker too.
                    listener.context = None
                    if not os.path.lexists(self.pending):
                        coolify._claim(self.pending)
                    raise coolify.PreconditionFailed() from None
                return {'status': 'rotated', 'serial': serial}
