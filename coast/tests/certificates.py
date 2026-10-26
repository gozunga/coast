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

"""Private test CA and leaves, including a due but valid current leaf."""

import datetime
from pathlib import Path
import ssl
import tempfile

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives import serialization
from cryptography import x509

from coast.common import tls
from coast.tests.unit import test_tls

SERVER_ID = '11111111-1111-1111-1111-111111111111'
ADDRESS = '192.0.2.10'


class Material:
    def prepare_material(self, server_id=SERVER_ID):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        test_tls.TLSTest._write_ca(self)
        self.ca_key = serialization.load_pem_private_key(
            (self.root / 'ca.key').read_bytes(), None)
        original = x509.load_pem_x509_certificate(
            (self.root / 'ca.pem').read_bytes())
        ca = (x509.CertificateBuilder().subject_name(original.subject)
              .issuer_name(original.issuer).public_key(original.public_key())
              .serial_number(original.serial_number)
              .not_valid_before(original.not_valid_before_utc)
              .not_valid_after(original.not_valid_after_utc)
              .add_extension(
                  x509.BasicConstraints(ca=True, path_length=0), True)
              .add_extension(x509.KeyUsage(
                  digital_signature=False, content_commitment=False,
                  key_encipherment=False, data_encipherment=False,
                  key_agreement=False, key_cert_sign=True, crl_sign=True,
                  encipher_only=False, decipher_only=False), True)
              .add_extension(x509.SubjectKeyIdentifier.from_public_key(
                  original.public_key()), False)
              .sign(self.ca_key, hashes.SHA256()))
        self.ca_pem = ca.public_bytes(serialization.Encoding.PEM)
        (self.root / 'ca.pem').write_bytes(self.ca_pem)
        self.server_id = server_id
        self.current = self.issue()
        # Do not change wall time: only the old leaf has used 90% of its life.
        self.current.certificate_pem = self.redate(
            self.current.certificate_pem, -9000, 1000)
        (self.root / 'server.crt').write_bytes(self.current.certificate_pem)
        (self.root / 'server.key').write_bytes(self.current.private_key_pem)
        (self.root / 'server.key').chmod(0o600)
        self.config.config(server_id=server_id, bind_ip=ADDRESS,
                           server_cert=str(self.root / 'server.crt'),
                           server_key=str(self.root / 'server.key'),
                           server_ca=str(self.root / 'ca.pem'),
                           client_ca=str(self.root / 'ca.pem'),
                           state_path=str(self.root / 'state'), group='agent')

    def issue(self, server_id=None, address=ADDRESS, usage='server'):
        return tls.issue_certificate(
            server_id or self.server_id, ip_addresses=[address],
            usage=usage, conf=self.conf)

    def redate(self, pem, before, after):
        original = x509.load_pem_x509_certificate(pem)
        now = datetime.datetime.now(datetime.timezone.utc)
        builder = (x509.CertificateBuilder().subject_name(original.subject)
                   .issuer_name(original.issuer)
                   .public_key(original.public_key())
                   .serial_number(original.serial_number)
                   .not_valid_before(now + datetime.timedelta(seconds=before))
                   .not_valid_after(now + datetime.timedelta(seconds=after)))
        for extension in original.extensions:
            builder = builder.add_extension(
                extension.value, extension.critical)
        return builder.sign(self.ca_key, hashes.SHA256()).public_bytes(
            serialization.Encoding.PEM)

    def body(self, issued=None):
        issued = issued or self.issue()
        return dict(expected_serial=str(x509.load_pem_x509_certificate(
            self.current.certificate_pem).serial_number),
                    certificate=issued.certificate_pem.decode('ascii'),
                    private_key=issued.private_key_pem.decode('ascii'))

    def peer_certificate(self, listener):
        context = ssl.create_default_context(cafile=self.conf.agent.server_ca)
        client = self.issue(usage='client')
        (self.root / 'client.crt').write_bytes(client.certificate_pem)
        (self.root / 'client.key').write_bytes(client.private_key_pem)
        context.load_cert_chain(self.root / 'client.crt',
                                self.root / 'client.key')
        peer_in, peer_out = ssl.MemoryBIO(), ssl.MemoryBIO()
        local_in, local_out = ssl.MemoryBIO(), ssl.MemoryBIO()
        peer = context.wrap_bio(peer_in, peer_out, server_hostname=ADDRESS)
        local = listener.context.wrap_bio(
            local_in, local_out, server_side=True)
        ready = [False, False]
        for step in range(30):
            for index, session in enumerate((peer, local)):
                if not ready[index]:
                    try:
                        session.do_handshake()
                        ready[index] = True
                    except ssl.SSLWantReadError:
                        pass
            local_in.write(peer_out.read())
            peer_in.write(local_out.read())
            if all(ready):
                return x509.load_der_x509_certificate(
                    peer.getpeercert(binary_form=True))
        self.fail('TLS handshake did not complete')
