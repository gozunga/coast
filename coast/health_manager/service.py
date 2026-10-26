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

"""oslo.service lifecycle for a single native-thread UDP receiver."""

import socket
import threading
import time

from oslo_config import cfg
from oslo_log import log as logging
from oslo_service import service
from oslo_utils import importutils

from coast.common import agent_auth
from coast.common import management
from coast.db import metrics
from coast.health_manager import certificates
from coast.health_manager import receiver
from coast.health_manager import stale

LOG = logging.getLogger(__name__)


class Service(service.Service):
    def __init__(self, conf=cfg.CONF, key_lookup=None):
        super().__init__()
        self.conf = conf
        self.key_lookup = key_lookup
        self.sock = None
        self.receiver = None
        self.thread = None
        self.detector = stale.Detector(conf)
        self.certificates = certificates.Rotation(conf)
        self.stopping = threading.Event()

    def prepare(self):
        # Run before launch: oslo.service can otherwise swallow startup
        # errors and let a misconfigured command exit successfully.
        # Check the agent credential before this handler hides the cause.
        agent_auth.prepare(self.conf)
        try:
            if self.key_lookup is None:
                if not self.conf.health_manager.key_lookup:
                    raise ValueError('Q86 key lookup is required')
                self.key_lookup = importutils.import_object(
                    self.conf.health_manager.key_lookup)
            if not callable(self.key_lookup):
                raise ValueError('Heartbeat key lookup must be callable')
            self.detector.key_lookup = self.key_lookup
            self.receiver = receiver.Receiver(self.key_lookup, self.conf)
            self.sock = management.socket_for_agent(
                self.conf, socket.SOCK_DGRAM,
                self.conf.health_manager.bind_port)
            self.sock.settimeout(0.5)
        except Exception:
            self.stop()
            raise ValueError('Health-manager startup failed; check management '
                             'namespace, bind IP and Q86 key lookup') from None

    def start(self):
        agent_auth.prepare(self.conf)
        super().start()
        try:
            if self.sock is None:
                self.prepare()
            self.thread = threading.Thread(target=self._run, daemon=True)
            self.thread.start()
            self.tg.add_timer_args(
                self.conf.metrics.cleanup_interval, self._maintain_metrics,
                initial_delay=self.conf.metrics.cleanup_interval)
            interval = self.conf.health_manager.heartbeat_interval
            self.tg.add_timer_args(interval, self._check_health,
                                   initial_delay=interval)
            self.tg.add_timer_args(
                self.conf.health_manager.cert_rotation_interval,
                self._rotate_certificates,
                initial_delay=self.conf.health_manager.cert_rotation_interval)
        except Exception:
            self.stop()
            raise ValueError('Health-manager startup failed; check management '
                             'namespace, bind IP and Q86 key lookup') from None

    def _maintain_metrics(self):
        try:
            metrics.maintain(self.conf)
        except Exception:
            LOG.warning('Metrics retention failed; will retry.')

    def _check_health(self):
        try:
            self.detector.run()
        except Exception:
            LOG.warning('Heartbeat stale check failed; will retry.')

    def _rotate_certificates(self):
        try:
            self.certificates.run()
        except Exception:
            LOG.warning('Certificate rotation sweep failed; will retry.')

    def _flush(self):
        try:
            self.receiver.flush()
        except Exception:
            LOG.warning('Heartbeat batch write failed; batch retained.')

    def _run(self):
        deadline = time.monotonic() + self.conf.health_manager.batch_interval
        while not self.stopping.is_set():
            try:
                packet, source = self.sock.recvfrom(
                    self.conf.health_manager.max_packet_size + 1)
                self.receiver.accept(packet, source[0])
            except socket.timeout:
                pass
            except OSError:
                LOG.warning('Heartbeat socket receive failed.')
                self.stopping.set()
            if time.monotonic() >= deadline:
                self._flush()
                deadline = (time.monotonic() +
                            self.conf.health_manager.batch_interval)
        self._flush()

    def stop(self, graceful=False):
        self.stopping.set()
        self.tg.stop_timers(wait=True)
        if self.thread:
            self.thread.join()
        if self.sock:
            self.sock.close()
        if self.receiver:
            self.receiver.close()
        super().stop(graceful=graceful)
