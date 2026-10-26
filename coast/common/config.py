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

from oslo_config import cfg
from oslo_db import options as db_options
from oslo_log import log as logging

from coast.common import opts
from coast import version

CONF = cfg.CONF


def parse_args(args=None, conf=CONF, **kwargs):
    opts.set_defaults(conf)
    opts.register_opts(conf)
    db_options.set_defaults(conf)
    logging.register_options(conf)
    conf(args=args, project='coast',
         version=version.version_info.version_string(), **kwargs)
    # oslo.config checks choices on first read. Read this one now so an
    # unsupported value stops the service at startup, not at export time.
    conf.export.upload_via


def setup_logging(conf=CONF):
    logging.setup(conf, 'coast')
