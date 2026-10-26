# Copyright 2026 Gozunga and Coast contributors
# SPDX-License-Identifier: Apache-2.0
"""kolla-ansible CLI plugin: `kolla-ansible coast-certificates`.

Install into the kolla-ansible virtualenv. The command runs Coast's
tools/kolla/ansible/certificates.yml, which calls tools/gen-ca.sh unchanged.
"""

import setuptools

setuptools.setup(
    name="coast-kolla-cli",
    version="0.1.0",
    license="Apache-2.0",
    packages=["coast_kolla_cli"],
    python_requires=">=3.10",
    entry_points={
        "kolla_ansible.cli": [
            "coast-certificates = coast_kolla_cli.commands:CoastCertificates",
        ],
    },
)
