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

"""oslo.service lifecycle for the conductor RPC consumer."""

import socket

from oslo_config import cfg
import oslo_messaging as messaging
from oslo_service import periodic_task
from oslo_service import service

from coast.common import agent_auth
from coast.common import exception
from coast.conductor import backups
from coast.conductor import engine_token_rotation
from coast.conductor import manager
from coast.conductor import rpcapi
from coast.conductor import usage


class DeploymentTasks(periodic_task.PeriodicTasks):
    def __init__(self, conf, endpoint):
        super().__init__(conf)
        self.endpoint = endpoint

    @periodic_task.periodic_task(spacing=1, run_immediately=True)
    def reconcile(self, context):
        self.endpoint.reconcile_deployments()


class SettleTasks(periodic_task.PeriodicTasks):
    def __init__(self, conf, endpoint):
        super().__init__(conf)
        self.endpoint = endpoint

    @periodic_task.periodic_task(spacing=1, run_immediately=True)
    def settle(self, context):
        self.endpoint.settle_deployments()


class ServiceTasks(periodic_task.PeriodicTasks):
    def __init__(self, conf, endpoint):
        super().__init__(conf)
        self.endpoint = endpoint

    @periodic_task.periodic_task(spacing=1, run_immediately=True)
    def reconcile(self, context):
        self.endpoint.reconcile_services()


class NetworkTasks(periodic_task.PeriodicTasks):
    def __init__(self, conf, endpoint):
        super().__init__(conf)
        self.endpoint = endpoint

    @periodic_task.periodic_task(spacing=1, run_immediately=True)
    def repair(self, context):
        self.endpoint.reconcile_networks()


class RegistryTasks(periodic_task.PeriodicTasks):
    def __init__(self, conf, endpoint):
        super().__init__(conf)
        self.endpoint = endpoint

    @periodic_task.periodic_task(spacing=1, run_immediately=True)
    def collect(self, context):
        self.endpoint.collect_registries()


class DomainTasks(periodic_task.PeriodicTasks):
    def __init__(self, conf, endpoint):
        super().__init__(conf)
        self.endpoint = endpoint

    @periodic_task.periodic_task(spacing=1, run_immediately=True)
    def drive(self, context):
        self.endpoint.drive_domains()


class Service(service.Service):
    def __init__(self, conf=cfg.CONF, endpoint=None):
        super().__init__()
        self.conf = conf
        self.endpoint = endpoint
        self.transport = None
        self.server = None

    def start(self):
        # Fail before oslo.service starts and before the handler below
        # replaces the cause with a generic conductor error.
        agent_auth.prepare(self.conf)
        super().start()
        try:
            self.endpoint = self.endpoint or manager.Manager(self.conf)
            self.endpoint.recover_stacks()
            tasks = DeploymentTasks(self.conf, self.endpoint)
            tasks.run_periodic_tasks(None)
            self.tg.add_timer_args(
                self.conf.conductor.deploy_reconcile_interval,
                tasks.run_periodic_tasks, args=[None],
                initial_delay=self.conf.conductor.deploy_reconcile_interval)
            settle_tasks = SettleTasks(self.conf, self.endpoint)
            settle_interval = self.conf.conductor.settle_poll_interval
            self.tg.add_timer_args(
                settle_interval, settle_tasks.run_periodic_tasks,
                args=[None], initial_delay=settle_interval)
            usage_tasks = usage.UsageTasks(
                self.conf, notifier=self.endpoint.notifier)
            usage_tasks.run_periodic_tasks(None)
            interval = self.conf.notifications.exists_notification_interval
            self.tg.add_timer_args(
                interval, usage_tasks.run_periodic_tasks, args=[None],
                initial_delay=interval)
            service_tasks = ServiceTasks(self.conf, self.endpoint)
            service_tasks.run_periodic_tasks(None)
            self.tg.add_timer_args(
                min(60, self.conf.conductor.service_reconcile_interval),
                service_tasks.run_periodic_tasks, args=[None],
                initial_delay=60)
            network_interval = (
                self.conf.controller_worker.network_repair_interval)
            if network_interval > 0:
                network_tasks = NetworkTasks(self.conf, self.endpoint)
                self.tg.add_timer_args(
                    network_interval, network_tasks.run_periodic_tasks,
                    args=[None], initial_delay=network_interval)
            registry_tasks = RegistryTasks(self.conf, self.endpoint)
            registry_tasks.run_periodic_tasks(None)
            registry_interval = (
                self.conf.controller_worker.registry_gc_interval)
            self.tg.add_timer_args(
                registry_interval, registry_tasks.run_periodic_tasks,
                args=[None], initial_delay=registry_interval)
            domain_tasks = DomainTasks(self.conf, self.endpoint)
            domain_tasks.run_periodic_tasks(None)
            domain_interval = self.conf.domains.poll_interval
            self.tg.add_timer_args(
                domain_interval, domain_tasks.run_periodic_tasks,
                args=[None], initial_delay=domain_interval)
            self.tg.add_timer_args(
                self.conf.backup.poll_interval, backups.tick,
                args=[self.conf], initial_delay=self.conf.backup.poll_interval)
            self.tg.add_timer_args(
                60, engine_token_rotation.tick, args=[self.endpoint],
                initial_delay=60)
            self.transport = messaging.get_rpc_transport(self.conf)
            self.server = messaging.get_rpc_server(
                self.transport, messaging.Target(topic=rpcapi.TOPIC,
                                                 server=socket.gethostname()),
                [self.endpoint],
                executor='threading', serializer=rpcapi.Serializer())
            self.server.start()
        except Exception:
            self.stop()
            raise exception.CoastException(
                'Conductor RPC startup failed.') from None

    def stop(self, graceful=False):
        # Stop the sweep before closing its endpoint and notification client.
        self.tg.stop_timers()
        self.tg.wait()
        if self.server:
            self.server.stop()
            self.server.wait()
        if self.transport:
            self.transport.cleanup()
        if self.endpoint:
            self.endpoint.close()
        super().stop(graceful=graceful)
