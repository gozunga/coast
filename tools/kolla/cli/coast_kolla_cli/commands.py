# Copyright 2026 Gozunga and Coast contributors
# SPDX-License-Identifier: Apache-2.0
"""Coast certificate command for the kolla-ansible CLI.

The Coast CA stays separate from the Kolla CA. This command only selects
Coast's certificate playbook and passes variables; tools/gen-ca.sh does the
work unchanged, including --check-expiry.
"""

import os

from cliff.command import Command
from kolla_ansible.cli.commands import KollaAnsibleMixin


def _tools_dir(parsed_args):
    tools = parsed_args.coast_tools_dir or os.environ.get("COAST_TOOLS_DIR")
    if not tools:
        raise SystemExit("coast-certificates: set --coast-tools-dir or "
                         "COAST_TOOLS_DIR to the Coast tree's tools directory")
    tools = os.path.abspath(tools)
    for rel in ("gen-ca.sh", "kolla/ansible/certificates.yml"):
        if not os.path.isfile(os.path.join(tools, rel)):
            raise SystemExit("coast-certificates: %s not found under %s"
                             % (rel, tools))
    return tools


class CoastCertificates(KollaAnsibleMixin, Command):
    """Generate Coast CA material or check its expiry"""

    def get_parser(self, prog_name):
        parser = super().get_parser(prog_name)
        group = parser.add_argument_group("Coast certificates action")
        group.add_argument(
            "--check-expiry", type=int,
            help="Read-only: fail if a certificate expires within the "
                 "given number of days, or material is missing")
        group.add_argument(
            "--coast-tools-dir",
            help="Coast tree tools directory (default: $COAST_TOOLS_DIR)")
        group.add_argument(
            "--coast-certificates-dir",
            default="/etc/kolla/config/coast/certs",
            help="Output directory for Coast CA material")
        return parser

    def take_action(self, parsed_args):
        tools = _tools_dir(parsed_args)
        extra_vars = {
            "kolla_action": "coast-certificates",
            "coast_ca_script": os.path.join(tools, "gen-ca.sh"),
            "coast_certificates_dir": parsed_args.coast_certificates_dir,
        }
        if parsed_args.check_expiry is not None:
            if parsed_args.check_expiry < 0:
                raise SystemExit("coast-certificates: --check-expiry must "
                                 "be zero or more days")
            self.app.LOG.info("Checking Coast certificate expiry")
            extra_vars["coast_check_expiry_days"] = parsed_args.check_expiry
        else:
            self.app.LOG.info("Generating Coast certificates")
        playbooks = parsed_args.playbook or [
            os.path.join(tools, "kolla/ansible/certificates.yml")]
        self.run_playbooks(parsed_args, playbooks, extra_vars=extra_vars)
