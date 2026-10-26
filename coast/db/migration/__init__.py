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

from alembic import config


def get_config(engine=None):
    conf = config.Config()
    conf.set_main_option(
        'script_location',
        str(Path(__file__).with_name('alembic_migrations')).replace('%', '%%'))
    if engine is not None:
        conf.attributes['engine'] = engine
    return conf
