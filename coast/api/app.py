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

"""Pecan application and Paste factories."""

from keystonemiddleware import auth_token
from oslo_config import cfg
from oslo_middleware import cors
from oslo_middleware import healthcheck
from paste import deploy
import pecan
from webob import exc

from coast.api import hooks
from coast.api import root_controller
from coast.api import webhooks


def setup_app(root=None):
    return pecan.make_app(
        root or root_controller.RootController(),
        hooks=[hooks.ContextHook(), hooks.PublicTextHook()], debug=False,
        force_canonical=False, wsme={'debug': False})


def factory(global_conf, **local_conf):
    root = (root_controller.V1Controller()
            if local_conf.get('versioned') == 'true' else None)
    return setup_app(root)


def auth_factory(global_conf, **local_conf):
    def wrap(application):
        authenticated = auth_token.AuthProtocol(application, local_conf)

        def dispatch(environ, start_response):
            # Git push webhooks authenticate with the token in the URL.
            if webhooks.matches(environ):
                return webhooks.handle(environ, start_response)
            if (environ.get('PATH_INFO') in ('', '/') and
                    environ['REQUEST_METHOD'] in ('GET', 'HEAD')):
                return application(environ, start_response)
            return authenticated(environ, start_response)

        return dispatch

    return wrap


def load_app():
    filename = cfg.CONF.find_file(cfg.CONF.api.api_paste_config)
    if filename is None:
        raise RuntimeError('API Paste configuration file was not found.')
    name = ('coast-noauth' if cfg.CONF.api.auth_strategy == 'noauth'
            else 'coast')
    return deploy.loadapp('config:' + filename, name=name)


def cors_factory(global_conf, **local_conf):
    # Use the service's parsed config, including explicit --config-file paths.
    # The stock Paste factory creates a separate ConfigOpts instance.
    return lambda application: cors.CORS(application, cfg.CONF)


def healthcheck_factory(global_conf, **local_conf):
    application = healthcheck.Healthcheck.app_factory(
        global_conf, **local_conf)

    def healthcheck_app(environ, start_response):
        if environ.get('PATH_INFO') not in ('', '/'):
            return exc.HTTPNotFound()(environ, start_response)
        if environ['REQUEST_METHOD'] not in ('GET', 'HEAD'):
            return exc.HTTPMethodNotAllowed(allow=['GET', 'HEAD'])(
                environ, start_response)
        return application(environ, start_response)

    return healthcheck_app
