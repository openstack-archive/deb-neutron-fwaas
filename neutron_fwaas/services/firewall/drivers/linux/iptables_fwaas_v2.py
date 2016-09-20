# Copyright (c) 2016
# All Rights Reserved.
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

from neutron.agent.linux import iptables_manager
from neutron.agent.linux import utils as linux_utils
from oslo_log import log as logging

from neutron_fwaas._i18n import _LE
from neutron_fwaas.extensions import firewall as fw_ext
from neutron_fwaas.services.firewall.drivers import fwaas_base_v2

LOG = logging.getLogger(__name__)
FWAAS_DRIVER_NAME = 'Fwaas iptables driver'
FWAAS_DEFAULT_CHAIN = 'fwaas-default-policy'

FWAAS_TO_IPTABLE_ACTION_MAP = {'allow': 'ACCEPT',
                               'deny': 'DROP',
                               'reject': 'REJECT'}
INGRESS_DIRECTION = 'ingress'
EGRESS_DIRECTION = 'egress'
CHAIN_NAME_PREFIX = {INGRESS_DIRECTION: 'i',
                     EGRESS_DIRECTION: 'o'}

""" Firewall rules are applied on internal-interfaces of Neutron router.
    The packets ingressing tenant's network will be on the output
    direction on internal-interfaces.
"""
IPTABLES_DIR = {INGRESS_DIRECTION: '-o',
                EGRESS_DIRECTION: '-i'}
IPV4 = 'ipv4'
IPV6 = 'ipv6'
IP_VER_TAG = {IPV4: 'v4',
              IPV6: 'v6'}

INTERNAL_DEV_PREFIX = 'qr-'
SNAT_INT_DEV_PREFIX = 'sg-'
ROUTER_2_FIP_DEV_PREFIX = 'rfp-'

MAX_INTF_NAME_LEN = 14


class IptablesFwaasDriver(fwaas_base_v2.FwaasDriverBase):
    """IPTables driver for Firewall As A Service."""

    def __init__(self):
        LOG.debug("Initializing fwaas iptables driver")
        self.pre_firewall = None

    def initialize(self):
        pass

    def _get_intf_name(self, if_prefix, port_id):
        _name = "%s%s" % (if_prefix, port_id)
        return _name[:MAX_INTF_NAME_LEN]

    def create_firewall_group(self, agent_mode, apply_list, firewall):
        LOG.debug('Creating firewall %(fw_id)s for tenant %(tid)s',
                  {'fw_id': firewall['id'], 'tid': firewall['tenant_id']})
        try:
            if firewall['admin_state_up']:
                self._setup_firewall(agent_mode, apply_list, firewall)
                self._remove_conntrack_new_firewall(agent_mode,
                                                    apply_list, firewall)
                self.pre_firewall = dict(firewall)
            else:
                self.apply_default_policy(agent_mode, apply_list, firewall)
        except (LookupError, RuntimeError):
            # catch known library exceptions and raise Fwaas generic exception
            LOG.exception(_LE("Failed to create firewall: %s"), firewall['id'])
            raise fw_ext.FirewallInternalDriverError(driver=FWAAS_DRIVER_NAME)

    def _get_ipt_mgrs_with_if_prefix(self, agent_mode, ri):
        """Gets the iptables manager along with the if prefix to apply rules.

        With DVR we can have differing namespaces depending on which agent
        (on Network or Compute node). Also, there is an associated i/f for
        each namespace. The iptables on the relevant namespace and matching
        i/f are provided. On the Network node we could have both the snat
        namespace and a fip so this is provided back as a list - so in that
        scenario rules can be applied on both.
        """
        if not ri.router.get('distributed'):
            return [{'ipt': ri.iptables_manager,
                     'if_prefix': INTERNAL_DEV_PREFIX}]
        ipt_mgrs = []
        # TODO(sridar): refactor to get strings to a common location.
        if agent_mode == 'dvr_snat':
            if ri.snat_iptables_manager:
                ipt_mgrs.append({'ipt': ri.snat_iptables_manager,
                                 'if_prefix': SNAT_INT_DEV_PREFIX})
        if ri.dist_fip_count:
            # handle the fip case on n/w or compute node.
            ipt_mgrs.append({'ipt': ri.iptables_manager,
                             'if_prefix': ROUTER_2_FIP_DEV_PREFIX})
        return ipt_mgrs

    def delete_firewall_group(self, agent_mode, apply_list, firewall):
        LOG.debug('Deleting firewall %(fw_id)s for tenant %(tid)s',
                  {'fw_id': firewall['id'], 'tid': firewall['tenant_id']})
        fwid = firewall['id']
        try:
            for ri, router_fw_ports in apply_list:
                ipt_if_prefix_list = self._get_ipt_mgrs_with_if_prefix(
                    agent_mode, ri)
                for ipt_if_prefix in ipt_if_prefix_list:
                    ipt_mgr = ipt_if_prefix['ipt']
                    self._remove_chains(fwid, ipt_mgr)
                    self._remove_default_chains(ipt_mgr)
                    # apply the changes immediately (no defer in firewall path)
                    ipt_mgr.defer_apply_off()
            self.pre_firewall = None
        except (LookupError, RuntimeError):
            # catch known library exceptions and raise Fwaas generic exception
            LOG.exception(_LE("Failed to delete firewall: %s"), fwid)
            raise fw_ext.FirewallInternalDriverError(driver=FWAAS_DRIVER_NAME)

    def update_firewall_group(self, agent_mode, apply_list, firewall):
        LOG.debug('Updating firewall %(fw_id)s for tenant %(tid)s',
                  {'fw_id': firewall['id'], 'tid': firewall['tenant_id']})
        try:
            if firewall['admin_state_up']:
                if self.pre_firewall:
                    self._remove_conntrack_updated_firewall(agent_mode,
                                    apply_list, self.pre_firewall, firewall)
                else:
                    self._remove_conntrack_new_firewall(agent_mode,
                                                    apply_list, firewall)
                self._setup_firewall(agent_mode, apply_list, firewall)
            else:
                self.apply_default_policy(agent_mode, apply_list, firewall)
            self.pre_firewall = dict(firewall)
        except (LookupError, RuntimeError):
            # catch known library exceptions and raise Fwaas generic exception
            LOG.exception(_LE("Failed to update firewall: %s"), firewall['id'])
            raise fw_ext.FirewallInternalDriverError(driver=FWAAS_DRIVER_NAME)

    def apply_default_policy(self, agent_mode, apply_list, firewall):
        LOG.debug('Applying firewall %(fw_id)s for tenant %(tid)s',
                  {'fw_id': firewall['id'], 'tid': firewall['tenant_id']})
        fwid = firewall['id']
        try:
            for ri, router_fw_ports in apply_list:
                ipt_if_prefix_list = self._get_ipt_mgrs_with_if_prefix(
                    agent_mode, ri)
                for ipt_if_prefix in ipt_if_prefix_list:
                    # the following only updates local memory; no hole in FW
                    ipt_mgr = ipt_if_prefix['ipt']
                    self._remove_chains(fwid, ipt_mgr)
                    self._remove_default_chains(ipt_mgr)

                    # create default 'DROP ALL' policy chain
                    self._add_default_policy_chain_v4v6(ipt_mgr)
                    self._enable_policy_chain(fwid, ipt_if_prefix,
                                              router_fw_ports)

                    # apply the changes immediately (no defer in firewall path)
                    ipt_mgr.defer_apply_off()
        except (LookupError, RuntimeError):
            # catch known library exceptions and raise Fwaas generic exception
            LOG.exception(
                _LE("Failed to apply default policy on firewall: %s"), fwid)
            raise fw_ext.FirewallInternalDriverError(driver=FWAAS_DRIVER_NAME)

    def _setup_firewall(self, agent_mode, apply_list, firewall):
        fwid = firewall['id']
        for ri, router_fw_ports in apply_list:
            ipt_if_prefix_list = self._get_ipt_mgrs_with_if_prefix(
                agent_mode, ri)
            for ipt_if_prefix in ipt_if_prefix_list:
                ipt_mgr = ipt_if_prefix['ipt']
                # the following only updates local memory; no hole in FW
                self._remove_chains(fwid, ipt_mgr)
                self._remove_default_chains(ipt_mgr)

                # create default 'DROP ALL' policy chain
                self._add_default_policy_chain_v4v6(ipt_mgr)
                # create chain based on configured policy
                self._setup_chains(firewall, ipt_if_prefix, router_fw_ports)

                # apply the changes immediately (no defer in firewall path)
                ipt_mgr.defer_apply_off()

    def _get_chain_name(self, fwid, ver, direction):
        return '%s%s%s' % (CHAIN_NAME_PREFIX[direction],
                           IP_VER_TAG[ver],
                           fwid)

    def _setup_chains(self, firewall, ipt_if_prefix, router_fw_ports):
        """Create Fwaas chain using the rules in the policy
        """
        egress_rule_list = firewall['egress_rule_list']
        ingress_rule_list = firewall['ingress_rule_list']
        fwid = firewall['id']
        ipt_mgr = ipt_if_prefix['ipt']

        # default rules for invalid packets and established sessions
        invalid_rule = self._drop_invalid_packets_rule()
        est_rule = self._allow_established_rule()

        for ver in [IPV4, IPV6]:
            if ver == IPV4:
                table = ipt_mgr.ipv4['filter']
            else:
                table = ipt_mgr.ipv6['filter']
            ichain_name = self._get_chain_name(fwid, ver, INGRESS_DIRECTION)
            ochain_name = self._get_chain_name(fwid, ver, EGRESS_DIRECTION)
            for name in [ichain_name, ochain_name]:
                table.add_chain(name)
                table.add_rule(name, invalid_rule)
                table.add_rule(name, est_rule)

        for rule in ingress_rule_list:
            if not rule['enabled']:
                continue
            iptbl_rule = self._convert_fwaas_to_iptables_rule(rule)
            if rule['ip_version'] == 4:
                ver = IPV4
                table = ipt_mgr.ipv4['filter']
            else:
                ver = IPV6
                table = ipt_mgr.ipv6['filter']
            ichain_name = self._get_chain_name(fwid, ver, INGRESS_DIRECTION)
            table.add_rule(ichain_name, iptbl_rule)

        for rule in egress_rule_list:
            if not rule['enabled']:
                continue
            iptbl_rule = self._convert_fwaas_to_iptables_rule(rule)
            if rule['ip_version'] == 4:
                ver = IPV4
                table = ipt_mgr.ipv4['filter']
            else:
                ver = IPV6
                table = ipt_mgr.ipv6['filter']
            ochain_name = self._get_chain_name(fwid, ver, EGRESS_DIRECTION)
            table.add_rule(ochain_name, iptbl_rule)

        self._enable_policy_chain(fwid, ipt_if_prefix, router_fw_ports)

    def _find_changed_rules(self, pre_firewall, firewall):
        """Find the rules changed between the current firewall
        and the updating rule
        """
        changed_rules = []
        for fw_rule_list in ['egress_rule_list', 'ingress_rule_list']:
            pre_fw_rules = pre_firewall[fw_rule_list]
            fw_rules = firewall[fw_rule_list]
            for pre_fw_rule in pre_fw_rules:
                for fw_rule in fw_rules:
                    if (pre_fw_rule.get('id') == fw_rule.get('id') and
                        pre_fw_rule != fw_rule):
                        changed_rules.append(pre_fw_rule)
                        changed_rules.append(fw_rule)
        return changed_rules

    def _find_removed_rules(self, pre_firewall, firewall):
        removed_rules = []
        for fw_rule_list in ['egress_rule_list', 'ingress_rule_list']:
            pre_fw_rules = pre_firewall[fw_rule_list]
            fw_rules = firewall[fw_rule_list]
            fw_rule_ids = [fw_rule['id'] for fw_rule in fw_rules]
            removed_rules.extend([pre_fw_rule for pre_fw_rule in pre_fw_rules
                    if pre_fw_rule['id'] not in fw_rule_ids])
        return removed_rules

    def _find_new_rules(self, pre_firewall, firewall):
        return self._find_removed_rules(firewall, pre_firewall)

    def _get_conntrack_cmd_from_rule(self, ipt_mgr, rule=None):
        prefixcmd = ['ip', 'netns', 'exec'] + [ipt_mgr.namespace]
        cmd = ['conntrack', '-D']
        if rule:
            conntrack_filter = self._get_conntrack_filter_from_rule(rule)
            exec_cmd = prefixcmd + cmd + conntrack_filter
        else:
            exec_cmd = prefixcmd + cmd
        return exec_cmd

    def _remove_conntrack_by_cmd(self, cmd):
        if cmd:
            try:
                linux_utils.execute(cmd, run_as_root=True,
                                    check_exit_code=True,
                                    extra_ok_codes=[1])
            except RuntimeError:
                LOG.exception(
                        _LE("Failed execute conntrack command %s"), str(cmd))

    def _remove_conntrack_new_firewall(self, agent_mode, apply_list, firewall):
        """Remove conntrack when create new firewall"""
        routers_list = list(set([apply_info[0] for apply_info in apply_list]))
        for ri in routers_list:
            ipt_if_prefix_list = self._get_ipt_mgrs_with_if_prefix(
                agent_mode, ri)
            for ipt_if_prefix in ipt_if_prefix_list:
                ipt_mgr = ipt_if_prefix['ipt']
                cmd = self._get_conntrack_cmd_from_rule(ipt_mgr)
                self._remove_conntrack_by_cmd(cmd)

    def _remove_conntrack_updated_firewall(self, agent_mode,
                                           apply_list, pre_firewall, firewall):
        """Remove conntrack when updated firewall"""
        routers_list = list(set([apply_info[0] for apply_info in apply_list]))
        for ri in routers_list:
            ipt_if_prefix_list = self._get_ipt_mgrs_with_if_prefix(
                agent_mode, ri)
            for ipt_if_prefix in ipt_if_prefix_list:
                ipt_mgr = ipt_if_prefix['ipt']
                ch_rules = self._find_changed_rules(pre_firewall,
                                                    firewall)
                i_rules = self._find_new_rules(pre_firewall, firewall)
                r_rules = self._find_removed_rules(pre_firewall, firewall)
                removed_conntrack_rules_list = ch_rules + i_rules + r_rules
                for rule in removed_conntrack_rules_list:
                    cmd = self._get_conntrack_cmd_from_rule(ipt_mgr, rule)
                    self._remove_conntrack_by_cmd(cmd)

    def _get_conntrack_filter_from_rule(self, rule):
        """Get conntrack filter from rule.
        The key for get conntrack filter is protocol, destination_port
        and source_port. If we want to take more keys, add to the list.
        """
        conntrack_filter = []
        keys = [['-p', 'protocol'], ['-f', 'ip_version'],
                ['--dport', 'destination_port'], ['--sport', 'source_port']]
        for key in keys:
            if rule.get(key[1]):
                if key[1] == 'ip_version':
                    conntrack_filter.append(key[0])
                    conntrack_filter.append('ipv' + str(rule.get(key[1])))
                else:
                    conntrack_filter.append(key[0])
                    conntrack_filter.append(rule.get(key[1]))
        return conntrack_filter

    def _remove_default_chains(self, nsid):
        """Remove fwaas default policy chain."""
        self._remove_chain_by_name(IPV4, FWAAS_DEFAULT_CHAIN, nsid)
        self._remove_chain_by_name(IPV6, FWAAS_DEFAULT_CHAIN, nsid)

    def _remove_chains(self, fwid, ipt_mgr):
        """Remove fwaas policy chain."""
        for ver in [IPV4, IPV6]:
            for direction in [INGRESS_DIRECTION, EGRESS_DIRECTION]:
                chain_name = self._get_chain_name(fwid, ver, direction)
                self._remove_chain_by_name(ver, chain_name, ipt_mgr)

    def _add_default_policy_chain_v4v6(self, ipt_mgr):
        ipt_mgr.ipv4['filter'].add_chain(FWAAS_DEFAULT_CHAIN)
        ipt_mgr.ipv4['filter'].add_rule(FWAAS_DEFAULT_CHAIN, '-j DROP')
        ipt_mgr.ipv6['filter'].add_chain(FWAAS_DEFAULT_CHAIN)
        ipt_mgr.ipv6['filter'].add_rule(FWAAS_DEFAULT_CHAIN, '-j DROP')

    def _remove_chain_by_name(self, ver, chain_name, ipt_mgr):
        if ver == IPV4:
            ipt_mgr.ipv4['filter'].remove_chain(chain_name)
        else:
            ipt_mgr.ipv6['filter'].remove_chain(chain_name)

    def _add_rules_to_chain(self, ipt_mgr, ver, chain_name, rules):
        if ver == IPV4:
            table = ipt_mgr.ipv4['filter']
        else:
            table = ipt_mgr.ipv6['filter']
        for rule in rules:
            table.add_rule(chain_name, rule)

    def _enable_policy_chain(self, fwid, ipt_if_prefix, router_fw_ports):
        bname = iptables_manager.binary_name
        ipt_mgr = ipt_if_prefix['ipt']
        if_prefix = ipt_if_prefix['if_prefix']

        for (ver, tbl) in [(IPV4, ipt_mgr.ipv4['filter']),
                           (IPV6, ipt_mgr.ipv6['filter'])]:
            for direction in [INGRESS_DIRECTION, EGRESS_DIRECTION]:
                chain_name = self._get_chain_name(fwid, ver, direction)
                chain_name = iptables_manager.get_chain_name(chain_name)
                if chain_name in tbl.chains:
                    for router_fw_port in router_fw_ports:
                        intf_name = self._get_intf_name(if_prefix,
                                                        router_fw_port)
                        jump_rule = ['%s %s -j %s-%s' % (
                            IPTABLES_DIR[direction], intf_name,
                            bname, chain_name)]
                        self._add_rules_to_chain(ipt_mgr, ver,
                                             'FORWARD', jump_rule)

        # jump to DROP_ALL policy
        chain_name = iptables_manager.get_chain_name(FWAAS_DEFAULT_CHAIN)
        for router_fw_port in router_fw_ports:
            intf_name = self._get_intf_name(if_prefix,
                                            router_fw_port)
            jump_rule = ['-o %s -j %s-%s' % (intf_name, bname, chain_name)]
            self._add_rules_to_chain(ipt_mgr, IPV4, 'FORWARD', jump_rule)
            self._add_rules_to_chain(ipt_mgr, IPV6, 'FORWARD', jump_rule)

        # jump to DROP_ALL policy
        chain_name = iptables_manager.get_chain_name(FWAAS_DEFAULT_CHAIN)
        for router_fw_port in router_fw_ports:
            intf_name = self._get_intf_name(if_prefix,
                                            router_fw_port)
            jump_rule = ['-i %s -j %s-%s' % (intf_name, bname, chain_name)]
            self._add_rules_to_chain(ipt_mgr, IPV4, 'FORWARD', jump_rule)
            self._add_rules_to_chain(ipt_mgr, IPV6, 'FORWARD', jump_rule)

    def _convert_fwaas_to_iptables_rule(self, rule):
        action = FWAAS_TO_IPTABLE_ACTION_MAP[rule.get('action')]

        args = [self._protocol_arg(rule.get('protocol')),
                self._port_arg('dport',
                               rule.get('protocol'),
                               rule.get('destination_port')),
                self._port_arg('sport',
                               rule.get('protocol'),
                               rule.get('source_port')),
                self._ip_prefix_arg('s', rule.get('source_ip_address')),
                self._ip_prefix_arg('d', rule.get('destination_ip_address')),
                self._action_arg(action)]

        iptables_rule = ' '.join(args)
        return iptables_rule

    def _drop_invalid_packets_rule(self):
        return '-m state --state INVALID -j DROP'

    def _allow_established_rule(self):
        return '-m state --state ESTABLISHED,RELATED -j ACCEPT'

    def _action_arg(self, action):
        if action:
            return '-j %s' % action
        return ''

    def _protocol_arg(self, protocol):
        if protocol:
            return '-p %s' % protocol
        return ''

    def _port_arg(self, direction, protocol, port):
        if not (protocol in ['udp', 'tcp'] and port):
            return ''
        return '--%s %s' % (direction, port)

    def _ip_prefix_arg(self, direction, ip_prefix):
        if ip_prefix:
            return '-%s %s' % (direction, ip_prefix)
        return ''
