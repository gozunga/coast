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

project = 'Coast'
copyright = '2026, Gozunga and Coast contributors'
extensions = [
    'openstackdocstheme',
    'myst_parser',
    'sphinxcontrib.mermaid',
]
myst_fence_as_directive = ['mermaid']
myst_heading_anchors = 3
source_suffix = {'.rst': 'restructuredtext', '.md': 'markdown'}
master_doc = 'index'
exclude_patterns = ['_build']
html_theme = 'openstackdocs'
openstackdocs_repo_name = 'coast'
openstackdocs_use_storyboard = False
