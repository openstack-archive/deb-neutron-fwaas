# Copyright 2016
# All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License"); you may
#  not use this file except in compliance with the License. You may obtain
#  a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#  WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#  License for the specific language governing permissions and limitations
#  under the License.

import mock
from neutron.api.v2 import attributes as attr
from neutron import context
from neutron import manager
from neutron.plugins.common import constants as const
from neutron.tests import fake_notifier
from neutron.tests.unit.extensions import test_l3 as test_l3_plugin
from oslo_config import cfg
import six

import neutron_fwaas.extensions
from neutron_fwaas.extensions import firewall_v2
from neutron_fwaas.services.firewall import fwaas_plugin_v2
from neutron_fwaas.tests import base
from neutron_fwaas.tests.unit.db.firewall.v2 import (
    test_firewall_db_v2 as test_db_firewall)

extensions_path = neutron_fwaas.extensions.__path__[0]

FW_PLUGIN_KLASS = (
    "neutron_fwaas.services.firewall.fwaas_plugin_v2.FirewallPluginV2"
)


class FirewallTestExtensionManager(test_l3_plugin.L3TestExtensionManager):

    def get_resources(self):
        res = super(FirewallTestExtensionManager, self).get_resources()
        res = res + firewall_v2.Firewall_v2.get_resources()
        return res

    def get_actions(self):
        return []

    def get_request_extensions(self):
        return []


class TestFirewallAgentApi(base.BaseTestCase):
    def setUp(self):
        super(TestFirewallAgentApi, self).setUp()

        self.api = fwaas_plugin_v2.FirewallAgentApi('topic', 'host')

    def test_init(self):
        self.assertEqual('topic', self.api.client.target.topic)
        self.assertEqual('host', self.api.host)

    def _call_test_helper(self, method_name):
        with mock.patch.object(self.api.client, 'cast') as rpc_mock, \
                mock.patch.object(self.api.client, 'prepare') as prepare_mock:
            prepare_mock.return_value = self.api.client
            getattr(self.api, method_name)(mock.sentinel.context, 'test')

        prepare_args = {'fanout': True}
        prepare_mock.assert_called_once_with(**prepare_args)

        rpc_mock.assert_called_once_with(mock.sentinel.context, method_name,
                                         firewall_group='test', host='host')

    def test_create_firewall_group(self):
        self._call_test_helper('create_firewall_group')

    def test_update_firewall_group(self):
        self._call_test_helper('update_firewall_group')

    def test_delete_firewall_group(self):
        self._call_test_helper('delete_firewall_group')


class TestFirewallRouterPortBase(
        test_db_firewall.FirewallPluginV2DbTestCase):

    def setUp(self, core_plugin=None, fw_plugin=None, ext_mgr=None):
        self.agentapi_del_fw_p = mock.patch(test_db_firewall.DELETEFW_PATH,
            create=True,
            new=test_db_firewall.FakeAgentApi().delete_firewall_group)
        self.agentapi_del_fw_p.start()

        # the plugin without L3 support
        plugin = 'neutron.tests.unit.extensions.test_l3.TestNoL3NatPlugin'
        # the L3 service plugin
        l3_plugin = ('neutron.tests.unit.extensions.test_l3.'
                     'TestL3NatServicePlugin')

        cfg.CONF.set_override('api_extensions_path', extensions_path)
        self.saved_attr_map = {}
        for resource, attrs in six.iteritems(attr.RESOURCE_ATTRIBUTE_MAP):
            self.saved_attr_map[resource] = attrs.copy()
        if not fw_plugin:
            fw_plugin = FW_PLUGIN_KLASS
        service_plugins = {'l3_plugin_name': l3_plugin,
            'fw_plugin_name': fw_plugin}

        if not ext_mgr:
            ext_mgr = FirewallTestExtensionManager()
        super(test_db_firewall.FirewallPluginV2DbTestCase, self).setUp(
            plugin=plugin, service_plugins=service_plugins, ext_mgr=ext_mgr)

        self.setup_notification_driver()

        self.l3_plugin = manager.NeutronManager.get_service_plugins().get(
            const.L3_ROUTER_NAT)
        self.plugin = manager.NeutronManager.get_service_plugins().get(
            'FIREWALL_V2')
        self.callbacks = self.plugin.endpoints[0]

    def restore_attribute_map(self):
        # Restore the original RESOURCE_ATTRIBUTE_MAP
        attr.RESOURCE_ATTRIBUTE_MAP = self.saved_attr_map

    def tearDown(self):
        self.restore_attribute_map()
        super(TestFirewallRouterPortBase, self).tearDown()


class TestFirewallCallbacks(TestFirewallRouterPortBase):

    def setUp(self):
        super(TestFirewallCallbacks,
              self).setUp(fw_plugin=FW_PLUGIN_KLASS)
        self.callbacks = self.plugin.endpoints[0]

    def test_set_firewall_group_status(self):
        ctx = context.get_admin_context()
        with self.firewall_policy() as fwp:
            fwp_id = fwp['firewall_policy']['id']
            with self.firewall_group(
                ingress_firewall_policy_id=fwp_id,
                admin_state_up=test_db_firewall.ADMIN_STATE_UP
            ) as fwg:
                fwg_id = fwg['firewall_group']['id']
                res = self.callbacks.set_firewall_group_status(ctx, fwg_id,
                                                         const.ACTIVE,
                                                         host='dummy')
                fwg_db = self.plugin.get_firewall_group(ctx, fwg_id)
                self.assertEqual(const.ACTIVE, fwg_db['status'])
                self.assertTrue(res)
                res = self.callbacks.set_firewall_group_status(ctx, fwg_id,
                                                         const.ERROR)
                fwg_db = self.plugin.get_firewall_group(ctx, fwg_id)
                self.assertEqual(const.ERROR, fwg_db['status'])
                self.assertFalse(res)


class TestFirewallPluginBasev2(TestFirewallRouterPortBase,
                             test_l3_plugin.L3NatTestCaseMixin):

    def setUp(self):
        super(TestFirewallPluginBasev2, self).setUp(fw_plugin=FW_PLUGIN_KLASS)
        fake_notifier.reset()

    def tearDown(self):
        super(TestFirewallPluginBasev2, self).tearDown()

    def test_create_firewall_group_ports_not_specified(self):
        """neutron firewall-create test-policy """
        with self.firewall_policy() as fwp:
            fwp_id = fwp['firewall_policy']['id']
            with self.firewall_group(
                name='test',
                ingress_firewall_policy_id=fwp_id,
                egress_firewall_policy_id=fwp_id,
                admin_state_up=True) as fwg1:
                self.assertEqual(const.INACTIVE,
                    fwg1['firewall_group']['status'])

    def test_create_firewall_group_with_ports(self):
        """neutron firewall_group create test-policy """
        with self.router(name='router1', admin_state_up=True,
            tenant_id=self._tenant_id) as r, \
                self.subnet() as s1, \
                self.subnet(cidr='20.0.0.0/24') as s2:

            body = self._router_interface_action(
                'add',
                r['router']['id'],
                s1['subnet']['id'],
                None)
            port_id1 = body['port_id']

            body = self._router_interface_action(
                'add',
                r['router']['id'],
                s2['subnet']['id'],
                None)
            port_id2 = body['port_id']
            fwg_ports = [port_id1, port_id2]
            with self.firewall_policy() as fwp:
                fwp_id = fwp['firewall_policy']['id']
                with self.firewall_group(
                    name='test',
                    ingress_firewall_policy_id=fwp_id,
                    egress_firewall_policy_id=fwp_id, ports=fwg_ports,
                    admin_state_up=True) as fwg1:
                    self.assertEqual(const.PENDING_CREATE,
                         fwg1['firewall_group']['status'])

            self._router_interface_action('remove',
                r['router']['id'],
                s1['subnet']['id'],
                None)
            self._router_interface_action(
                'remove',
                r['router']['id'],
                s2['subnet']['id'],
                None)

    def test_create_firewall_group_with_ports_on_diff_routers(self):
        """neutron firewall_group create test-policy """
        with self.router(name='router1', admin_state_up=True,
            tenant_id=self._tenant_id) as r, \
                self.subnet() as s1, \
                self.subnet(cidr='20.0.0.0/24') as s2:
            body = self._router_interface_action(
                'add',
                r['router']['id'],
                s1['subnet']['id'],
                None)
            port_id1 = body['port_id']
            body = self._router_interface_action(
                'add',
                r['router']['id'],
                s2['subnet']['id'],
                None)
            port_id2 = body['port_id']

            with self.router(name='router1', admin_state_up=True,
                tenant_id=self._tenant_id) as r2, \
                    self.subnet() as s3:

                body = self._router_interface_action(
                    'add',
                    r2['router']['id'],
                    s3['subnet']['id'],
                    None)
                port_id3 = body['port_id']

                fwg_ports = [port_id1, port_id2, port_id3]
                with self.firewall_policy() as fwp:
                    fwp_id = fwp['firewall_policy']['id']
                    with self.firewall_group(
                        name='test',
                        ingress_firewall_policy_id=fwp_id,
                        egress_firewall_policy_id=fwp_id,
                        ports=fwg_ports,
                        admin_state_up=True) as fwg1:
                        self.assertEqual(const.PENDING_CREATE,
                            fwg1['firewall_group']['status'])

                self._router_interface_action('remove',
                    r2['router']['id'],
                    s3['subnet']['id'],
                    None)

            self._router_interface_action('remove',
                r['router']['id'],
                s1['subnet']['id'],
                None)
            self._router_interface_action(
                'remove',
                r['router']['id'],
                s2['subnet']['id'],
                None)

    def test_create_firewall_group_with_ports_no_policy(self):
        """neutron firewall_group create test-policy """
        with self.router(name='router1', admin_state_up=True,
            tenant_id=self._tenant_id) as r, \
                self.subnet() as s1, \
                self.subnet(cidr='20.0.0.0/24') as s2:

            body = self._router_interface_action(
                'add',
                r['router']['id'],
                s1['subnet']['id'],
                None)
            port_id1 = body['port_id']

            body = self._router_interface_action(
                'add',
                r['router']['id'],
                s2['subnet']['id'],
                None)
            port_id2 = body['port_id']
            fwg_ports = [port_id1, port_id2]
            with self.firewall_group(
                name='test',
                default_policy=False,
                ports=fwg_ports,
                admin_state_up=True) as fwg1:
                self.assertEqual(const.INACTIVE,
                     fwg1['firewall_group']['status'])

            self._router_interface_action('remove',
                r['router']['id'],
                s1['subnet']['id'],
                None)
            self._router_interface_action(
                'remove',
                r['router']['id'],
                s2['subnet']['id'],
                None)

    def test_update_firewall_group_with_new_ports_no_polcy(self):
        """neutron firewall_group create test-policy """
        with self.router(name='router1', admin_state_up=True,
            tenant_id=self._tenant_id) as r, \
                self.subnet() as s1, \
                self.subnet(cidr='20.0.0.0/24') as s2, \
                self.subnet(cidr='30.0.0.0/24') as s3:

            body = self._router_interface_action(
                'add',
                r['router']['id'],
                s1['subnet']['id'],
                None)
            port_id1 = body['port_id']

            body = self._router_interface_action(
                'add',
                r['router']['id'],
                s2['subnet']['id'],
                None)
            port_id2 = body['port_id']

            body = self._router_interface_action(
                'add',
                r['router']['id'],
                s3['subnet']['id'],
                None)
            port_id3 = body['port_id']

            fwg_ports = [port_id1, port_id2]
            with self.firewall_group(
                name='test',
                default_policy=False,
                ports=fwg_ports,
                admin_state_up=True) as fwg1:
                self.assertEqual(const.INACTIVE,
                     fwg1['firewall_group']['status'])
                data = {'firewall_group': {'ports': [port_id2, port_id3]}}
                req = self.new_update_request('firewall_groups', data,
                                              fwg1['firewall_group']['id'])
                res = self.deserialize(self.fmt,
                                       req.get_response(self.ext_api))

                self.assertEqual(sorted([port_id2, port_id3]),
                                 sorted(res['firewall_group']['ports']))

            self._router_interface_action('remove',
                r['router']['id'],
                s1['subnet']['id'],
                None)
            self._router_interface_action(
                'remove',
                r['router']['id'],
                s2['subnet']['id'],
                None)

    def test_update_firewall_group_with_new_ports_status_pending(self):
        """neutron firewall_group create test-policy """
        with self.router(name='router1', admin_state_up=True,
            tenant_id=self._tenant_id) as r, \
                self.subnet() as s1, \
                self.subnet(cidr='20.0.0.0/24') as s2, \
                self.subnet(cidr='30.0.0.0/24') as s3:

            body = self._router_interface_action(
                'add',
                r['router']['id'],
                s1['subnet']['id'],
                None)
            port_id1 = body['port_id']

            body = self._router_interface_action(
                'add',
                r['router']['id'],
                s2['subnet']['id'],
                None)
            port_id2 = body['port_id']
            fwg_ports = [port_id1, port_id2]

            body = self._router_interface_action(
                'add',
                r['router']['id'],
                s3['subnet']['id'],
                None)
            port_id3 = body['port_id']

            with self.firewall_policy() as fwp:
                fwp_id = fwp['firewall_policy']['id']
                with self.firewall_group(
                    name='test',
                    ingress_firewall_policy_id=fwp_id,
                    egress_firewall_policy_id=fwp_id, ports=fwg_ports,
                    admin_state_up=True) as fwg1:
                    self.assertEqual(const.PENDING_CREATE,
                         fwg1['firewall_group']['status'])
                    data = {'firewall_group': {'ports': [port_id2, port_id3]}}
                    req = self.new_update_request('firewall_groups', data,
                                                  fwg1['firewall_group']['id'])
                    res = req.get_response(self.ext_api)
                    self.assertEqual(409, res.status_int)
            self._router_interface_action('remove',
                r['router']['id'],
                s1['subnet']['id'],
                None)
            self._router_interface_action(
                'remove',
                r['router']['id'],
                s2['subnet']['id'],
                None)

    def test_update_firewall_group_with_new_ports_status_active(self):
        """neutron firewall_group create test-policy """
        with self.router(name='router1', admin_state_up=True,
            tenant_id=self._tenant_id) as r, \
                self.subnet() as s1, \
                self.subnet(cidr='20.0.0.0/24') as s2, \
                self.subnet(cidr='30.0.0.0/24') as s3:

            body = self._router_interface_action(
                'add',
                r['router']['id'],
                s1['subnet']['id'],
                None)
            port_id1 = body['port_id']

            body = self._router_interface_action(
                'add',
                r['router']['id'],
                s2['subnet']['id'],
                None)
            port_id2 = body['port_id']
            fwg_ports = [port_id1, port_id2]

            body = self._router_interface_action(
                'add',
                r['router']['id'],
                s3['subnet']['id'],
                None)
            port_id3 = body['port_id']

            with self.firewall_policy() as fwp:
                fwp_id = fwp['firewall_policy']['id']
                with self.firewall_group(
                    name='test',
                    ingress_firewall_policy_id=fwp_id,
                    egress_firewall_policy_id=fwp_id, ports=fwg_ports,
                    admin_state_up=True) as fwg1:
                    self.assertEqual(const.PENDING_CREATE,
                         fwg1['firewall_group']['status'])

                    ctx = context.get_admin_context()
                    self.callbacks.set_firewall_group_status(ctx,
                        fwg1['firewall_group']['id'], const.ACTIVE)
                    data = {'firewall_group': {'ports': [port_id2, port_id3]}}
                    req = self.new_update_request('firewall_groups', data,
                                                  fwg1['firewall_group']['id'])
                    res = self.deserialize(self.fmt,
                                           req.get_response(self.ext_api))
                    self.assertEqual(sorted([port_id2, port_id3]),
                                     sorted(res['firewall_group']['ports']))

            self._router_interface_action('remove',
                r['router']['id'],
                s1['subnet']['id'],
                None)
            self._router_interface_action(
                'remove',
                r['router']['id'],
                s2['subnet']['id'],
                None)
