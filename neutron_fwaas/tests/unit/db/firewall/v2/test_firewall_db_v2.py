# Copyright (c) 2016 OpenStack Foundation
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

import contextlib

import mock
from neutron.api import extensions as api_ext
from neutron.common import config
from neutron import context
import neutron.extensions as nextensions
from neutron import manager
from neutron.plugins.common import constants
from oslo_config import cfg
from oslo_utils import importutils
from oslo_utils import uuidutils
import six
import testtools
import webob.exc

from neutron_fwaas.db.firewall.v2 import firewall_db_v2 as fdb
from neutron_fwaas import extensions
from neutron_fwaas.extensions import firewall_v2 as firewall
from neutron_fwaas.services.firewall import fwaas_plugin_v2
from neutron_fwaas.tests import base

DB_FW_PLUGIN_KLASS = (
    "neutron_fwaas.db.firewall.v2.firewall_db_v2.Firewall_db_mixin_v2"
)
FWAAS_PLUGIN = 'neutron_fwaas.services.firewall.fwaas_plugin_v2'
DELETEFW_PATH = FWAAS_PLUGIN + '.FirewallAgentApi.delete_firewall_group'
extensions_path = ':'.join(extensions.__path__ + nextensions.__path__)
DESCRIPTION = 'default description'
PUBLIC = True
PROTOCOL = 'tcp'
IP_VERSION = 4
SOURCE_IP_ADDRESS_RAW = '1.1.1.1'
DESTINATION_IP_ADDRESS_RAW = '2.2.2.2'
SOURCE_PORT = '55000:56000'
DESTINATION_PORT = '56000:57000'
ACTION = 'allow'
AUDITED = True
ENABLED = True
ADMIN_STATE_UP = True


class FakeAgentApi(fwaas_plugin_v2.FirewallCallbacks):
    """
    This class used to mock the AgentAPI delete method inherits from
    FirewallCallbacks because it needs access to the firewall_deleted method.
    The delete_firewall method belongs to the FirewallAgentApi, which has
    no access to the firewall_deleted method normally because it's not
    responsible for deleting the firewall from the DB. However, it needs
    to in the unit tests since there is no agent to call back.
    """
    def __init__(self):
        pass

    def delete_firewall_group(self, context, firewall_group, **kwargs):
        self.plugin = (manager.NeutronManager.
            get_service_plugins()['FIREWALL_V2'])
        self.firewall_group_deleted(context, firewall_group['id'], **kwargs)


class FirewallPluginV2DbTestCase(base.NeutronDbPluginV2TestCase):
    resource_prefix_map = dict(
        (k, firewall.FIREWALL_PREFIX)
        for k in firewall.RESOURCE_ATTRIBUTE_MAP.keys()
    )

    def setUp(self, core_plugin=None, fw_plugin=None, ext_mgr=None):
        self.agentapi_delf_p = mock.patch(
            DELETEFW_PATH, create=True,
            new=FakeAgentApi().delete_firewall_group)
        self.agentapi_delf_p.start()
        if not fw_plugin:
            fw_plugin = DB_FW_PLUGIN_KLASS
        service_plugins = {'fw_plugin_name': fw_plugin}

        fdb.Firewall_db_mixin_v2.supported_extension_aliases = ["fwaas_v2"]
        fdb.Firewall_db_mixin_v2.path_prefix = firewall.FIREWALL_PREFIX
        super(FirewallPluginV2DbTestCase, self).setUp(
            ext_mgr=ext_mgr,
            service_plugins=service_plugins
        )

        if not ext_mgr:
            self.plugin = importutils.import_object(fw_plugin)
            ext_mgr = api_ext.PluginAwareExtensionManager(
                extensions_path,
                {constants.FIREWALL: self.plugin}
            )
            app = config.load_paste_app('extensions_test_app')
            self.ext_api = api_ext.ExtensionMiddleware(app, ext_mgr=ext_mgr)

    def _test_list_resources(self, resource, items,
                             neutron_context=None,
                             query_params=None):
        if resource.endswith('y'):
            resource_plural = resource.replace('y', 'ies')
        else:
            resource_plural = resource + 's'

        res = self._list(resource_plural,
                         neutron_context=neutron_context,
                         query_params=query_params)
        resource = resource.replace('-', '_')
        self.assertEqual(
            sorted([i[resource]['id'] for i in items]),
            sorted([i['id'] for i in res[resource_plural]]))

    def _get_test_firewall_rule_attrs(self, name='firewall_rule1'):
        attrs = {'name': name,
                 'tenant_id': self._tenant_id,
                 'public': PUBLIC,
                 'protocol': PROTOCOL,
                 'ip_version': IP_VERSION,
                 'source_ip_address': SOURCE_IP_ADDRESS_RAW,
                 'destination_ip_address': DESTINATION_IP_ADDRESS_RAW,
                 'source_port': SOURCE_PORT,
                 'destination_port': DESTINATION_PORT,
                 'action': ACTION,
                 'enabled': ENABLED}
        return attrs

    def _get_test_firewall_policy_attrs(self, name='firewall_policy1',
                                        audited=AUDITED):
        attrs = {'name': name,
                 'description': DESCRIPTION,
                 'tenant_id': self._tenant_id,
                 'public': PUBLIC,
                 'firewall_rules': [],
                 'audited': audited}
        return attrs

    def _get_test_firewall_group_attrs(self, name='firewall_1',
                                 status='PENDING_CREATE'):
        attrs = {'name': name,
                 'tenant_id': self._tenant_id,
                 'admin_state_up': ADMIN_STATE_UP,
                 'status': status}

        return attrs

    def _create_firewall_policy(self, fmt, name, description, public,
                                firewall_rules, audited,
                                expected_res_status=None, **kwargs):
        tenant_id = kwargs.get('tenant_id', self._tenant_id)
        data = {'firewall_policy': {'name': name,
                                    'description': description,
                                    'tenant_id': tenant_id,
                                    'public': public,
                                    'firewall_rules': firewall_rules,
                                    'audited': audited}}

        fw_policy_req = self.new_create_request('firewall_policies', data, fmt)
        fw_policy_res = fw_policy_req.get_response(self.ext_api)
        if expected_res_status:
            self.assertEqual(expected_res_status, fw_policy_res.status_int)

        return fw_policy_res

    def _replace_firewall_status(self, attrs, old_status, new_status):
        if attrs['status'] is old_status:
            attrs['status'] = new_status
        return attrs

    @contextlib.contextmanager
    def firewall_policy(self, fmt=None, name='firewall_policy1',
                        description=DESCRIPTION, public=True,
                        firewall_rules=None, audited=True,
                        do_delete=True, **kwargs):
        if firewall_rules is None:
            firewall_rules = []
        if not fmt:
            fmt = self.fmt
        res = self._create_firewall_policy(fmt, name, description, public,
                                           firewall_rules, audited,
                                           **kwargs)
        if res.status_int >= 400:
            raise webob.exc.HTTPClientError(code=res.status_int)
        firewall_policy = self.deserialize(fmt or self.fmt, res)
        yield firewall_policy
        if do_delete:
            self._delete('firewall_policies',
                         firewall_policy['firewall_policy']['id'])

    def _create_firewall_rule(self, fmt, name, public, protocol,
                              ip_version, source_ip_address,
                              destination_ip_address, source_port,
                              destination_port, action, enabled,
                              expected_res_status=None, **kwargs):
        tenant_id = kwargs.get('tenant_id', self._tenant_id)
        data = {'firewall_rule': {'name': name,
                                  'tenant_id': tenant_id,
                                  'public': public,
                                  'protocol': protocol,
                                  'ip_version': ip_version,
                                  'source_ip_address': source_ip_address,
                                  'destination_ip_address':
                                  destination_ip_address,
                                  'source_port': source_port,
                                  'destination_port': destination_port,
                                  'action': action,
                                  'enabled': enabled}}

        fw_rule_req = self.new_create_request('firewall_rules', data, fmt)
        fw_rule_res = fw_rule_req.get_response(self.ext_api)
        if expected_res_status:
            self.assertEqual(expected_res_status, fw_rule_res.status_int)

        return fw_rule_res

    @contextlib.contextmanager
    def firewall_rule(self, fmt=None, name='firewall_rule1',
                      public=PUBLIC, protocol=PROTOCOL, ip_version=IP_VERSION,
                      source_ip_address=SOURCE_IP_ADDRESS_RAW,
                      destination_ip_address=DESTINATION_IP_ADDRESS_RAW,
                      source_port=SOURCE_PORT,
                      destination_port=DESTINATION_PORT,
                      action=ACTION, enabled=ENABLED,
                      do_delete=True, **kwargs):
        if not fmt:
            fmt = self.fmt
        res = self._create_firewall_rule(fmt, name, public, protocol,
                                         ip_version, source_ip_address,
                                         destination_ip_address,
                                         source_port, destination_port,
                                         action, enabled, **kwargs)
        if res.status_int >= 400:
            raise webob.exc.HTTPClientError(code=res.status_int)
        firewall_rule = self.deserialize(fmt or self.fmt, res)
        yield firewall_rule
        if do_delete:
            self._delete('firewall_rules',
                         firewall_rule['firewall_rule']['id'])

    def _create_firewall_group(self, fmt, name, description,
                               ingress_firewall_policy_id,
                               egress_firewall_policy_id,
                               ports=None, admin_state_up=True,
                               expected_res_status=None, **kwargs):
        tenant_id = kwargs.get('tenant_id', self._tenant_id)
        if ingress_firewall_policy_id is None:
            default_policy = kwargs.get('default_policy', True)
            if default_policy:
                res = self._create_firewall_policy(fmt, 'fwp',
                                                   description=DESCRIPTION,
                                                   public=True,
                                                   firewall_rules=[],
                                                   tenant_id=tenant_id,
                                                   audited=AUDITED)
                firewall_policy = self.deserialize(fmt or self.fmt, res)
                fwp_id = firewall_policy["firewall_policy"]["id"]
                ingress_firewall_policy_id = fwp_id
        data = {'firewall_group': {'name': name,
                     'description': description,
                     'ingress_firewall_policy_id': ingress_firewall_policy_id,
                     'egress_firewall_policy_id': egress_firewall_policy_id,
                     'admin_state_up': admin_state_up}}
        ctx = kwargs.get('context', None)
        if ctx is None or ctx.is_admin:
            data['firewall_group'].update({'tenant_id': tenant_id})
        if ports is not None:
            data['firewall_group'].update({'ports': ports})

        firewall_req = self.new_create_request('firewall_groups', data, fmt,
                                               context=ctx)
        firewall_res = firewall_req.get_response(self.ext_api)
        if expected_res_status:
            self.assertEqual(expected_res_status, firewall_res.status_int)

        return firewall_res

    @contextlib.contextmanager
    def firewall_group(self, fmt=None, name='firewall_1',
                       description=DESCRIPTION,
                       ingress_firewall_policy_id=None,
                       egress_firewall_policy_id=None,
                       ports=None, admin_state_up=True,
                       do_delete=True, **kwargs):
        if not fmt:
            fmt = self.fmt
        res = self._create_firewall_group(fmt, name, description,
                                          ingress_firewall_policy_id,
                                          egress_firewall_policy_id,
                                          ports=ports,
                                          admin_state_up=admin_state_up,
                                          **kwargs)
        if res.status_int >= 400:
            raise webob.exc.HTTPClientError(code=res.status_int)
        firewall_group = self.deserialize(fmt or self.fmt, res)
        yield firewall_group
        if do_delete:
            self._delete('firewall_groups',
                         firewall_group['firewall_group']['id'])

    def _rule_action(self, action, id, firewall_rule_id, insert_before=None,
                     insert_after=None, expected_code=webob.exc.HTTPOk.code,
                     expected_body=None, body_data=None):
        # We intentionally do this check for None since we want to distinguish
        # from empty dictionary
        if body_data is None:
            if action == 'insert':
                body_data = {'firewall_rule_id': firewall_rule_id,
                             'insert_before': insert_before,
                             'insert_after': insert_after}
            else:
                body_data = {'firewall_rule_id': firewall_rule_id}

        req = self.new_action_request('firewall_policies',
                                      body_data, id,
                                      "%s_rule" % action)
        res = req.get_response(self.ext_api)
        self.assertEqual(expected_code, res.status_int)
        response = self.deserialize(self.fmt, res)
        if expected_body:
            self.assertEqual(expected_body, response)
        return response

    def _compare_firewall_rule_lists(self, firewall_policy_id,
                                     observed_list, expected_list):
        position = 0
        for r1, r2 in zip(observed_list, expected_list):
            rule = r1['firewall_rule']
            rule['firewall_policy_id'] = firewall_policy_id
            position += 1
            rule['position'] = position
            for k in rule:
                self.assertEqual(r2[k], rule[k])


class TestFirewallDBPluginV2(FirewallPluginV2DbTestCase):

    def test_create_firewall_policy(self):
        name = "firewall_policy1"
        attrs = self._get_test_firewall_policy_attrs(name)

        with self.firewall_policy(name=name, public=PUBLIC,
                                  firewall_rules=None,
                                  audited=AUDITED) as firewall_policy:
            for k, v in six.iteritems(attrs):
                self.assertEqual(v, firewall_policy['firewall_policy'][k])

    def test_create_firewall_policy_with_rules(self):
        name = "firewall_policy1"
        attrs = self._get_test_firewall_policy_attrs(name)

        with self.firewall_rule(name='fwr1') as fwr1, \
                self.firewall_rule(name='fwr2') as fwr2, \
                self.firewall_rule(name='fwr3') as fwr3:
            fr = [fwr1, fwr2, fwr3]
            fw_rule_ids = [r['firewall_rule']['id'] for r in fr]
            attrs['firewall_rules'] = fw_rule_ids
            with self.firewall_policy(name=name, public=PUBLIC,
                                      firewall_rules=fw_rule_ids,
                                      audited=AUDITED) as fwp:
                for k, v in six.iteritems(attrs):
                    self.assertEqual(v, fwp['firewall_policy'][k])

    def test_create_admin_firewall_policy_with_other_tenant_rules(self):
        with self.firewall_rule(public=False) as fr:
            fw_rule_ids = [fr['firewall_rule']['id']]
            res = self._create_firewall_policy(None, 'firewall_policy1',
                                               description=DESCRIPTION,
                                               public=PUBLIC,
                                               firewall_rules=fw_rule_ids,
                                               audited=AUDITED,
                                               tenant_id='admin-tenant')
            self.assertEqual(webob.exc.HTTPConflict.code, res.status_int)

    def test_create_firewall_policy_with_previously_associated_rule(self):
        with self.firewall_rule() as fwr:
            fw_rule_ids = [fwr['firewall_rule']['id']]
            with self.firewall_policy(firewall_rules=fw_rule_ids):
                with self.firewall_policy(firewall_rules=fw_rule_ids,
                                          public=PUBLIC) as fwp2:
                    self.assertEqual(
                        fwr['firewall_rule']['id'],
                        fwp2['firewall_policy']['firewall_rules'][0])

    def test_create_public_firewall_policy_with_nonpublic_rule(self):
        with self.firewall_rule(public=False) as fwr:
            fw_rule_ids = [fwr['firewall_rule']['id']]
            res = self._create_firewall_policy(
                None, 'firewall_policy1', description=DESCRIPTION, public=True,
                firewall_rules=fw_rule_ids, audited=AUDITED)
            self.assertEqual(webob.exc.HTTPConflict.code, res.status_int)

    def test_show_firewall_policy(self):
        name = "firewall_policy1"
        attrs = self._get_test_firewall_policy_attrs(name)

        with self.firewall_policy(name=name, public=PUBLIC,
                                  firewall_rules=None,
                                  audited=AUDITED) as fwp:
            req = self.new_show_request('firewall_policies',
                                        fwp['firewall_policy']['id'],
                                        fmt=self.fmt)
            res = self.deserialize(self.fmt, req.get_response(self.ext_api))
            for k, v in six.iteritems(attrs):
                self.assertEqual(v, res['firewall_policy'][k])

    def test_list_firewall_policies(self):
        with self.firewall_policy(name='fwp1', description='fwp') as fwp1, \
                self.firewall_policy(name='fwp2', description='fwp') as fwp2, \
                self.firewall_policy(name='fwp3', description='fwp') as fwp3:
            fw_policies = [fwp1, fwp2, fwp3]
            self._test_list_resources('firewall_policy',
                                      fw_policies,
                                      query_params='description=fwp')

    def test_update_firewall_policy(self):
        name = "new_firewall_policy1"
        attrs = self._get_test_firewall_policy_attrs(name, audited=False)

        with self.firewall_policy(public=PUBLIC,
                                  firewall_rules=None,
                                  audited=AUDITED) as fwp:
            data = {'firewall_policy': {'name': name}}
            req = self.new_update_request('firewall_policies', data,
                                          fwp['firewall_policy']['id'])
            res = self.deserialize(self.fmt, req.get_response(self.ext_api))
            for k, v in six.iteritems(attrs):
                self.assertEqual(v, res['firewall_policy'][k])

    def _test_update_firewall_policy(self, with_audited):
        with self.firewall_policy(name='firewall_policy1',
                                  description='fwp',
                                  audited=AUDITED) as fwp:
            attrs = self._get_test_firewall_policy_attrs(audited=with_audited)
            data = {'firewall_policy':
                    {'description': 'fw_p1'}}
            if with_audited:
                data['firewall_policy']['audited'] = 'True'

            req = self.new_update_request('firewall_policies', data,
                                          fwp['firewall_policy']['id'])
            res = self.deserialize(self.fmt,
                                   req.get_response(self.ext_api))
            attrs['description'] = 'fw_p1'
            for k, v in six.iteritems(attrs):
                self.assertEqual(v, res['firewall_policy'][k])

    def test_update_firewall_policy_set_audited_false(self):
        self._test_update_firewall_policy(with_audited=False)

    def test_update_firewall_policy_with_audited_set_true(self):
        self._test_update_firewall_policy(with_audited=True)

    def test_update_firewall_policy_with_rules(self):
        attrs = self._get_test_firewall_policy_attrs()

        with self.firewall_rule(name='fwr1') as fwr1, \
                self.firewall_rule(name='fwr2') as fwr2, \
                self.firewall_rule(name='fwr3') as fwr3:
            with self.firewall_policy() as fwp:
                fr = [fwr1, fwr2, fwr3]
                fw_rule_ids = [r['firewall_rule']['id'] for r in fr]
                attrs['firewall_rules'] = fw_rule_ids
                data = {'firewall_policy':
                        {'firewall_rules': fw_rule_ids}}
                req = self.new_update_request('firewall_policies', data,
                                              fwp['firewall_policy']['id'])
                res = self.deserialize(self.fmt,
                                       req.get_response(self.ext_api))
                attrs['audited'] = False
                attrs['firewall_rules'] = sorted(attrs['firewall_rules'])
                # TODO(sridar): set it so that the ordering is maintained
                res['firewall_policy']['firewall_rules'] = sorted(
                    res['firewall_policy']['firewall_rules'])
                for k, v in six.iteritems(attrs):
                    self.assertEqual(v, res['firewall_policy'][k])

    def test_update_firewall_policy_replace_rules(self):
        attrs = self._get_test_firewall_policy_attrs()

        with self.firewall_rule(name='fwr1') as fwr1, \
                self.firewall_rule(name='fwr2') as fwr2, \
                self.firewall_rule(name='fwr3') as fwr3, \
                self.firewall_rule(name='fwr4') as fwr4:
            frs = [fwr1, fwr2, fwr3, fwr4]
            fr1 = frs[0:2]
            fr2 = frs[2:4]
            with self.firewall_policy() as fwp:
                fw_rule_ids = [r['firewall_rule']['id'] for r in fr1]
                data = {'firewall_policy':
                        {'firewall_rules': fw_rule_ids}}
                req = self.new_update_request('firewall_policies', data,
                                              fwp['firewall_policy']['id'])
                req.get_response(self.ext_api)

                fw_rule_ids = [r['firewall_rule']['id'] for r in fr2]
                attrs['firewall_rules'] = fw_rule_ids
                new_data = {'firewall_policy':
                            {'firewall_rules': fw_rule_ids}}
                req = self.new_update_request('firewall_policies', new_data,
                                              fwp['firewall_policy']['id'])
                res = self.deserialize(self.fmt,
                                       req.get_response(self.ext_api))
                attrs['audited'] = False
                for k, v in six.iteritems(attrs):
                    self.assertEqual(v, res['firewall_policy'][k])

    @testtools.skip('bug/1614673')
    def test_update_firewall_policy_reorder_rules(self):
        attrs = self._get_test_firewall_policy_attrs()

        with self.firewall_rule(name='fwr1') as fwr1, \
                self.firewall_rule(name='fwr2') as fwr2, \
                self.firewall_rule(name='fwr3') as fwr3, \
                self.firewall_rule(name='fwr4') as fwr4:
            fr = [fwr1, fwr2, fwr3, fwr4]
            with self.firewall_policy() as fwp:
                fw_rule_ids = [fr[2]['firewall_rule']['id'],
                               fr[3]['firewall_rule']['id']]
                data = {'firewall_policy':
                        {'firewall_rules': fw_rule_ids}}
                req = self.new_update_request('firewall_policies', data,
                                              fwp['firewall_policy']['id'])
                req.get_response(self.ext_api)
                # shuffle the rules, add more rules
                fw_rule_ids = [fr[1]['firewall_rule']['id'],
                               fr[3]['firewall_rule']['id'],
                               fr[2]['firewall_rule']['id'],
                               fr[0]['firewall_rule']['id']]
                attrs['firewall_rules'] = fw_rule_ids
                data = {'firewall_policy':
                        {'firewall_rules': fw_rule_ids}}
                req = self.new_update_request('firewall_policies', data,
                                              fwp['firewall_policy']['id'])
                res = self.deserialize(self.fmt,
                                       req.get_response(self.ext_api))
                rules = []
                for rule_id in fw_rule_ids:
                    req = self.new_show_request('firewall_rules',
                                                rule_id,
                                                fmt=self.fmt)
                    res = self.deserialize(self.fmt,
                                           req.get_response(self.ext_api))
                    rules.append(res['firewall_rule'])
                self.assertEqual(1, rules[0]['position'])
                self.assertEqual(fr[1]['firewall_rule']['id'], rules[0]['id'])
                self.assertEqual(2, rules[1]['position'])
                self.assertEqual(fr[3]['firewall_rule']['id'], rules[1]['id'])
                self.assertEqual(3, rules[2]['position'])
                self.assertEqual(fr[2]['firewall_rule']['id'], rules[2]['id'])
                self.assertEqual(4, rules[3]['position'])
                self.assertEqual(fr[0]['firewall_rule']['id'], rules[3]['id'])

    def test_update_firewall_policy_with_non_existing_rule(self):
        attrs = self._get_test_firewall_policy_attrs()

        with self.firewall_rule(name='fwr1') as fwr1, \
                self.firewall_rule(name='fwr2') as fwr2:
            fr = [fwr1, fwr2]
            with self.firewall_policy() as fwp:
                fw_rule_ids = [r['firewall_rule']['id'] for r in fr]
                # appending non-existent rule
                fw_rule_ids.append(uuidutils.generate_uuid())
                data = {'firewall_policy':
                        {'firewall_rules': fw_rule_ids}}
                req = self.new_update_request('firewall_policies', data,
                                              fwp['firewall_policy']['id'])
                res = req.get_response(self.ext_api)
                # check that the firewall_rule was not found
                self.assertEqual(404, res.status_int)
                # check if none of the rules got added to the policy
                req = self.new_show_request('firewall_policies',
                                            fwp['firewall_policy']['id'],
                                            fmt=self.fmt)
                res = self.deserialize(self.fmt,
                                       req.get_response(self.ext_api))
                for k, v in six.iteritems(attrs):
                    self.assertEqual(v, res['firewall_policy'][k])

    def test_update_public_firewall_policy_with_nonpublic_rule(self):
        with self.firewall_rule(name='fwr1', public=False) as fr:
            with self.firewall_policy() as fwp:
                fw_rule_ids = [fr['firewall_rule']['id']]
                # update public policy with nonpublic rule
                data = {'firewall_policy':
                        {'firewall_rules': fw_rule_ids}}
                req = self.new_update_request('firewall_policies', data,
                                              fwp['firewall_policy']['id'])
                res = req.get_response(self.ext_api)
                self.assertEqual(webob.exc.HTTPConflict.code, res.status_int)

    def test_update_firewall_policy_with_public_attr_nonpublic_rule(self):
        with self.firewall_rule(name='fwr1', public=False) as fr:
            with self.firewall_policy(public=False) as fwp:
                fw_rule_ids = [fr['firewall_rule']['id']]
                # update public policy with public attr and nonpublic rule
                data = {'firewall_policy': {'public': True,
                                            'firewall_rules': fw_rule_ids}}
                req = self.new_update_request('firewall_policies', data,
                                              fwp['firewall_policy']['id'])
                res = req.get_response(self.ext_api)
                self.assertEqual(webob.exc.HTTPConflict.code, res.status_int)

    def test_update_firewall_policy_with_public_attr_exist_unshare_rule(self):
        with self.firewall_rule(name='fwr1', public=False) as fr:
            fw_rule_ids = [fr['firewall_rule']['id']]
            with self.firewall_policy(public=False,
                                      firewall_rules=fw_rule_ids) as fwp:
                # update policy with public attr
                data = {'firewall_policy': {'public': True}}
                req = self.new_update_request('firewall_policies', data,
                                              fwp['firewall_policy']['id'])
                res = req.get_response(self.ext_api)
                self.assertEqual(webob.exc.HTTPConflict.code, res.status_int)

    def test_update_firewall_policy_assoc_with_other_tenant_firewall(self):
        with self.firewall_policy(public=True, tenant_id='tenant1') as fwp:
            fwp_id = fwp['firewall_policy']['id']
            with self.firewall_group(ingress_firewall_policy_id=fwp_id,
                    egress_firewall_policy_id=fwp_id):
                data = {'firewall_policy': {'public': False}}
                req = self.new_update_request('firewall_policies', data,
                                              fwp['firewall_policy']['id'])
                res = req.get_response(self.ext_api)
                self.assertEqual(webob.exc.HTTPConflict.code, res.status_int)

    def test_delete_firewall_policy(self):
        ctx = context.get_admin_context()
        with self.firewall_policy(do_delete=False) as fwp:
            fwp_id = fwp['firewall_policy']['id']
            req = self.new_delete_request('firewall_policies', fwp_id)
            res = req.get_response(self.ext_api)
            self.assertEqual(204, res.status_int)
            self.assertRaises(firewall.FirewallPolicyNotFound,
                              self.plugin.get_firewall_policy,
                              ctx, fwp_id)

    @testtools.skip('bug/1614673')
    def test_delete_firewall_policy_with_rule(self):
        ctx = context.get_admin_context()
        attrs = self._get_test_firewall_policy_attrs()
        with self.firewall_policy(do_delete=False) as fwp:
            fwp_id = fwp['firewall_policy']['id']
            with self.firewall_rule(name='fwr1') as fr:
                fr_id = fr['firewall_rule']['id']
                fw_rule_ids = [fr_id]
                attrs['firewall_rules'] = fw_rule_ids
                data = {'firewall_policy':
                        {'firewall_rules': fw_rule_ids}}
                req = self.new_update_request('firewall_policies', data,
                                              fwp['firewall_policy']['id'])
                req.get_response(self.ext_api)
                fw_rule = self.plugin.get_firewall_rule(ctx, fr_id)
                self.assertEqual(fwp_id, fw_rule['ingress_firewall_policy_id'])
                req = self.new_delete_request('firewall_policies', fwp_id)
                res = req.get_response(self.ext_api)
                self.assertEqual(204, res.status_int)
                self.assertRaises(firewall.FirewallPolicyNotFound,
                                  self.plugin.get_firewall_policy,
                                  ctx, fwp_id)
                fw_rule = self.plugin.get_firewall_rule(ctx, fr_id)
                self.assertIsNone(fw_rule['ingress_firewall_policy_id'])

    def test_delete_firewall_policy_with_firewall_group_association(self):
        attrs = self._get_test_firewall_group_attrs()
        with self.firewall_policy() as fwp:
            fwp_id = fwp['firewall_policy']['id']
            attrs['firewall_policy_id'] = fwp_id
            with self.firewall_group(
                    ingress_firewall_policy_id=fwp_id,
                    admin_state_up=ADMIN_STATE_UP):
                req = self.new_delete_request('firewall_policies', fwp_id)
                res = req.get_response(self.ext_api)
                self.assertEqual(409, res.status_int)

    def test_create_firewall_rule(self):
        attrs = self._get_test_firewall_rule_attrs()

        with self.firewall_rule() as firewall_rule:
            for k, v in six.iteritems(attrs):
                self.assertEqual(v, firewall_rule['firewall_rule'][k])

        attrs['source_port'] = None
        attrs['destination_port'] = None
        with self.firewall_rule(source_port=None,
                                destination_port=None) as firewall_rule:
            for k, v in six.iteritems(attrs):
                self.assertEqual(v, firewall_rule['firewall_rule'][k])

        attrs['source_port'] = '10000'
        attrs['destination_port'] = '80'
        with self.firewall_rule(source_port=10000,
                                destination_port=80) as firewall_rule:
            for k, v in six.iteritems(attrs):
                self.assertEqual(v, firewall_rule['firewall_rule'][k])

        attrs['source_port'] = '10000'
        attrs['destination_port'] = '80'
        with self.firewall_rule(source_port='10000',
                                destination_port='80') as firewall_rule:
            for k, v in six.iteritems(attrs):
                self.assertEqual(v, firewall_rule['firewall_rule'][k])

    def test_create_firewall_src_port_illegal_range(self):
        attrs = self._get_test_firewall_rule_attrs()
        attrs['source_port'] = '65535:1024'
        res = self._create_firewall_rule(self.fmt, **attrs)
        self.assertEqual(400, res.status_int)

    def test_create_firewall_dest_port_illegal_range(self):
        attrs = self._get_test_firewall_rule_attrs()
        attrs['destination_port'] = '65535:1024'
        res = self._create_firewall_rule(self.fmt, **attrs)
        self.assertEqual(400, res.status_int)

    def test_create_firewall_rule_icmp_with_port(self):
        attrs = self._get_test_firewall_rule_attrs()
        attrs['protocol'] = 'icmp'
        res = self._create_firewall_rule(self.fmt, **attrs)
        self.assertEqual(400, res.status_int)

    def test_create_firewall_rule_icmp_without_port(self):
        attrs = self._get_test_firewall_rule_attrs()

        attrs['protocol'] = 'icmp'
        attrs['source_port'] = None
        attrs['destination_port'] = None
        with self.firewall_rule(source_port=None,
                                destination_port=None,
                                protocol='icmp') as firewall_rule:
            for k, v in six.iteritems(attrs):
                self.assertEqual(v, firewall_rule['firewall_rule'][k])

    def test_create_firewall_without_source(self):
        attrs = self._get_test_firewall_rule_attrs()
        attrs['source_ip_address'] = None
        res = self._create_firewall_rule(self.fmt, **attrs)
        self.assertEqual(201, res.status_int)

    def test_create_firewall_rule_without_destination(self):
        attrs = self._get_test_firewall_rule_attrs()
        attrs['destination_ip_address'] = None
        res = self._create_firewall_rule(self.fmt, **attrs)
        self.assertEqual(201, res.status_int)

    def test_create_firewall_rule_without_protocol_with_dport(self):
        attrs = self._get_test_firewall_rule_attrs()
        attrs['protocol'] = None
        attrs['source_port'] = None
        res = self._create_firewall_rule(self.fmt, **attrs)
        self.assertEqual(400, res.status_int)

    def test_create_firewall_rule_without_protocol_with_sport(self):
        attrs = self._get_test_firewall_rule_attrs()
        attrs['protocol'] = None
        attrs['destination_port'] = None
        res = self._create_firewall_rule(self.fmt, **attrs)
        self.assertEqual(400, res.status_int)

    def test_show_firewall_rule_with_fw_policy_not_associated(self):
        attrs = self._get_test_firewall_rule_attrs()
        with self.firewall_rule() as fw_rule:
            req = self.new_show_request('firewall_rules',
                                        fw_rule['firewall_rule']['id'],
                                        fmt=self.fmt)
            res = self.deserialize(self.fmt,
                                   req.get_response(self.ext_api))
            for k, v in six.iteritems(attrs):
                self.assertEqual(v, res['firewall_rule'][k])

    @testtools.skip('bug/1614673')
    def test_show_firewall_rule_with_fw_policy_associated(self):
        attrs = self._get_test_firewall_rule_attrs()
        with self.firewall_rule() as fw_rule:
            with self.firewall_policy() as fwp:
                fwp_id = fwp['firewall_policy']['id']
                attrs['ingress_firewall_policy_id'] = fwp_id
                data = {'firewall_policy':
                        {'firewall_rules':
                         [fw_rule['firewall_rule']['id']]}}
                req = self.new_update_request('firewall_policies', data,
                                              fwp['firewall_policy']['id'])
                req.get_response(self.ext_api)
                req = self.new_show_request('firewall_rules',
                                            fw_rule['firewall_rule']['id'],
                                            fmt=self.fmt)
                res = self.deserialize(self.fmt,
                                       req.get_response(self.ext_api))
                for k, v in six.iteritems(attrs):
                    self.assertEqual(v, res['firewall_rule'][k])

    def test_create_firewall_rule_with_ipv6_addrs_and_wrong_ip_version(self):
        attrs = self._get_test_firewall_rule_attrs()
        attrs['source_ip_address'] = '::/0'
        attrs['destination_ip_address'] = '2001:db8:3::/64'
        attrs['ip_version'] = 4
        res = self._create_firewall_rule(self.fmt, **attrs)
        self.assertEqual(400, res.status_int)

        attrs = self._get_test_firewall_rule_attrs()
        attrs['source_ip_address'] = None
        attrs['destination_ip_address'] = '2001:db8:3::/64'
        attrs['ip_version'] = 4
        res = self._create_firewall_rule(self.fmt, **attrs)
        self.assertEqual(400, res.status_int)

        attrs = self._get_test_firewall_rule_attrs()
        attrs['source_ip_address'] = '::/0'
        attrs['destination_ip_address'] = None
        attrs['ip_version'] = 4
        res = self._create_firewall_rule(self.fmt, **attrs)
        self.assertEqual(400, res.status_int)

    def test_list_firewall_rules(self):
        with self.firewall_rule(name='fwr1') as fwr1, \
                self.firewall_rule(name='fwr2') as fwr2, \
                self.firewall_rule(name='fwr3') as fwr3:
            fr = [fwr1, fwr2, fwr3]
            query_params = 'protocol=tcp'
            self._test_list_resources('firewall_rule', fr,
                                      query_params=query_params)

    def test_update_firewall_rule(self):
        name = "new_firewall_rule1"
        attrs = self._get_test_firewall_rule_attrs(name)

        attrs['source_port'] = '10:20'
        attrs['destination_port'] = '30:40'
        with self.firewall_rule() as fwr:
            data = {'firewall_rule': {'name': name,
                                      'source_port': '10:20',
                                      'destination_port': '30:40'}}
            req = self.new_update_request('firewall_rules', data,
                                          fwr['firewall_rule']['id'])
            res = self.deserialize(self.fmt,
                                   req.get_response(self.ext_api))
            for k, v in six.iteritems(attrs):
                self.assertEqual(v, res['firewall_rule'][k])

        attrs['source_port'] = '10000'
        attrs['destination_port'] = '80'
        with self.firewall_rule() as fwr:
            data = {'firewall_rule': {'name': name,
                                      'source_port': 10000,
                                      'destination_port': 80}}
            req = self.new_update_request('firewall_rules', data,
                                          fwr['firewall_rule']['id'])
            res = self.deserialize(self.fmt,
                                   req.get_response(self.ext_api))
            for k, v in six.iteritems(attrs):
                self.assertEqual(v, res['firewall_rule'][k])

        attrs['source_port'] = '10000'
        attrs['destination_port'] = '80'
        with self.firewall_rule() as fwr:
            data = {'firewall_rule': {'name': name,
                                      'source_port': '10000',
                                      'destination_port': '80'}}
            req = self.new_update_request('firewall_rules', data,
                                          fwr['firewall_rule']['id'])
            res = self.deserialize(self.fmt,
                                   req.get_response(self.ext_api))
            for k, v in six.iteritems(attrs):
                self.assertEqual(v, res['firewall_rule'][k])

        attrs['source_port'] = None
        attrs['destination_port'] = None
        with self.firewall_rule() as fwr:
            data = {'firewall_rule': {'name': name,
                                      'source_port': None,
                                      'destination_port': None}}
            req = self.new_update_request('firewall_rules', data,
                                          fwr['firewall_rule']['id'])
            res = self.deserialize(self.fmt,
                                   req.get_response(self.ext_api))
            for k, v in six.iteritems(attrs):
                self.assertEqual(v, res['firewall_rule'][k])

    def test_update_firewall_rule_with_port_and_no_proto(self):
        with self.firewall_rule() as fwr:
            data = {'firewall_rule': {'protocol': None,
                                      'destination_port': 80}}
            req = self.new_update_request('firewall_rules', data,
                                          fwr['firewall_rule']['id'])
            res = req.get_response(self.ext_api)
            self.assertEqual(400, res.status_int)

    def test_update_firewall_rule_without_ports_and_no_proto(self):
        with self.firewall_rule() as fwr:
            data = {'firewall_rule': {'protocol': None,
                                      'destination_port': None,
                                      'source_port': None}}
            req = self.new_update_request('firewall_rules', data,
                                          fwr['firewall_rule']['id'])
            res = req.get_response(self.ext_api)
            self.assertEqual(200, res.status_int)

    def test_update_firewall_rule_with_port(self):
        with self.firewall_rule(source_port=None,
                                destination_port=None,
                                protocol=None) as fwr:
            data = {'firewall_rule': {'destination_port': 80}}
            req = self.new_update_request('firewall_rules', data,
                                          fwr['firewall_rule']['id'])
            res = req.get_response(self.ext_api)
            self.assertEqual(400, res.status_int)

    def test_update_firewall_rule_with_port_illegal_range(self):
        with self.firewall_rule() as fwr:
            data = {'firewall_rule': {'destination_port': '65535:1024'}}
            req = self.new_update_request('firewall_rules', data,
                                          fwr['firewall_rule']['id'])
            res = req.get_response(self.ext_api)
            self.assertEqual(400, res.status_int)

    def test_update_firewall_rule_with_port_and_protocol(self):
        with self.firewall_rule(source_port=None,
                                destination_port=None,
                                protocol=None) as fwr:
            data = {'firewall_rule': {'destination_port': 80,
                                      'protocol': 'tcp'}}
            req = self.new_update_request('firewall_rules', data,
                                          fwr['firewall_rule']['id'])
            res = req.get_response(self.ext_api)
            self.assertEqual(200, res.status_int)

    def test_update_firewall_rule_with_policy_associated(self):
        name = "new_firewall_rule1"
        attrs = self._get_test_firewall_rule_attrs(name)
        with self.firewall_rule() as fwr:
            with self.firewall_policy() as fwp:
                fwr_id = fwr['firewall_rule']['id']
                data = {'firewall_policy': {'firewall_rules': [fwr_id]}}
                req = self.new_update_request('firewall_policies', data,
                                              fwp['firewall_policy']['id'])
                req.get_response(self.ext_api)
                data = {'firewall_rule': {'name': name}}
                req = self.new_update_request('firewall_rules', data,
                                              fwr['firewall_rule']['id'])
                res = self.deserialize(self.fmt,
                                       req.get_response(self.ext_api))
                for k, v in six.iteritems(attrs):
                    self.assertEqual(v, res['firewall_rule'][k])
                req = self.new_show_request('firewall_policies',
                                            fwp['firewall_policy']['id'],
                                            fmt=self.fmt)
                res = self.deserialize(self.fmt,
                                       req.get_response(self.ext_api))
                self.assertEqual(
                    [fwr_id],
                    res['firewall_policy']['firewall_rules'])
                self.assertFalse(res['firewall_policy']['audited'])

    @testtools.skip('bug/1614680')
    def test_update_firewall_rule_associated_with_other_tenant_policy(self):
        with self.firewall_rule(public=True, tenant_id='tenant1') as fwr:
            fwr_id = [fwr['firewall_rule']['id']]
            with self.firewall_policy(public=False,
                                      firewall_rules=fwr_id):
                data = {'firewall_rule': {'public': False}}
                req = self.new_update_request('firewall_rules', data,
                                              fwr['firewall_rule']['id'])
                res = req.get_response(self.ext_api)
                self.assertEqual(webob.exc.HTTPConflict.code, res.status_int)

    def test_delete_firewall_rule(self):
        ctx = context.get_admin_context()
        with self.firewall_rule(do_delete=False) as fwr:
            fwr_id = fwr['firewall_rule']['id']
            req = self.new_delete_request('firewall_rules', fwr_id)
            res = req.get_response(self.ext_api)
            self.assertEqual(204, res.status_int)
            self.assertRaises(firewall.FirewallRuleNotFound,
                              self.plugin.get_firewall_rule,
                              ctx, fwr_id)

    def test_delete_firewall_rule_with_policy_associated(self):
        with self.firewall_rule() as fwr:
            with self.firewall_policy() as fwp:
                fwr_id = fwr['firewall_rule']['id']
                data = {'firewall_policy': {'firewall_rules': [fwr_id]}}
                req = self.new_update_request('firewall_policies', data,
                                              fwp['firewall_policy']['id'])
                res = req.get_response(self.ext_api)
                req = self.new_delete_request('firewall_rules', fwr_id)
                res = req.get_response(self.ext_api)
                self.assertEqual(409, res.status_int)

    def _test_create_firewall_group(self, attrs):
        with self.firewall_policy() as fwp:
            fwp_id = fwp['firewall_policy']['id']
            attrs['ingress_firewall_policy_id'] = fwp_id
            attrs['egress_firewall_policy_id'] = fwp_id
            with self.firewall_group(
                name=attrs['name'],
                ingress_firewall_policy_id=fwp_id,
                egress_firewall_policy_id=fwp_id,
                admin_state_up=ADMIN_STATE_UP
            ) as firewall_group:
                for k, v in six.iteritems(attrs):
                    self.assertEqual(v, firewall_group['firewall_group'][k])

    def test_create_firewall_group(self):
        attrs = self._get_test_firewall_group_attrs("firewall1")
        self._test_create_firewall_group(attrs)

    def test_create_firewall_group_with_dvr(self):
        cfg.CONF.set_override('router_distributed', True)
        attrs = self._get_test_firewall_group_attrs("firewall1", "CREATED")
        self._test_create_firewall_group(attrs)

    def test_create_firewall_group_with_fwp_does_not_exist(self):
        fmt = self.fmt
        fwg_name = "firewall1"
        description = "my_firewall1"
        not_found_fwp_id = uuidutils.generate_uuid()
        self._create_firewall_group(fmt, fwg_name,
                              description, not_found_fwp_id,
                              not_found_fwp_id, ports=None,
                              admin_state_up=ADMIN_STATE_UP,
                              expected_res_status=404)

    def test_create_firewall_group_with_fwp_on_different_tenant(self):
        fmt = self.fmt
        fwg_name = "firewall1"
        description = "my_firewall1"
        with self.firewall_policy(public=False, tenant_id='tenant2') as fwp:
            fwp_id = fwp['firewall_policy']['id']
            ctx = context.Context('not_admin', 'tenant1')
            self._create_firewall_group(fmt, fwg_name,
                                        description,
                    ingress_firewall_policy_id=fwp_id,
                                        egress_firewall_policy_id=fwp_id,
                    context=ctx,
                                        expected_res_status=404)

    def test_create_firewall_group_with_admin_and_fwp_different_tenant(self):
        fmt = self.fmt
        fwg_name = "firewall1"
        description = "my_firewall1"
        with self.firewall_policy(public=False, tenant_id='tenant2') as fwp:
            fwp_id = fwp['firewall_policy']['id']
            ctx = context.get_admin_context()
            self._create_firewall_group(fmt, fwg_name,
                                        description, fwp_id, fwp_id,
                                        tenant_id="admin-tenant",
                                        context=ctx,
                                        expected_res_status=409)

    def test_create_firewall_group_with_admin_and_fwp_is_public(self):
        fwg_name = "fw_with_public_fwp"
        with self.firewall_policy(tenant_id="tenantX") as fwp:
            fwp_id = fwp['firewall_policy']['id']
            ctx = context.get_admin_context()
            target_tenant = 'tenant1'
            with self.firewall_group(name=fwg_name,
                                     ingress_firewall_policy_id=fwp_id,
                                     tenant_id=target_tenant, context=ctx,
                                     admin_state_up=ADMIN_STATE_UP) as fwg:
                self.assertEqual(target_tenant,
                                 fwg['firewall_group']['tenant_id'])

    def test_show_firewall_group(self):
        name = "firewall1"
        attrs = self._get_test_firewall_group_attrs(name)

        with self.firewall_policy() as fwp:
            fwp_id = fwp['firewall_policy']['id']
            attrs['ingress_firewall_policy_id'] = fwp_id
            attrs['egress_firewall_policy_id'] = fwp_id
            attrs['status'] = 'PENDING_CREATE'
            with self.firewall_group(
                    name=name,
                    ingress_firewall_policy_id=fwp_id,
                    egress_firewall_policy_id=fwp_id,
                    admin_state_up=ADMIN_STATE_UP) as firewall_group:
                req = self.new_show_request(
                    'firewall_groups',
                    firewall_group['firewall_group']['id'],
                    fmt=self.fmt)
                res = self.deserialize(self.fmt,
                                       req.get_response(self.ext_api))
                for k, v in six.iteritems(attrs):
                    self.assertEqual(v, res['firewall_group'][k])

    def test_list_firewall_groups(self):
        with self.firewall_policy() as fwp:
            fwp_id = fwp['firewall_policy']['id']
            with self.firewall_group(name='fwg1', tenant_id='tenant1',
                               ingress_firewall_policy_id=fwp_id,
                               description='fwg') as fwg1, \
                    self.firewall_group(name='fwg2', tenant_id='tenant2',
                                  ingress_firewall_policy_id=fwp_id,
                                  egress_firewall_policy_id=fwp_id,
                                  description='fwg') as fwg2, \
                    self.firewall_group(name='fwg3', tenant_id='tenant3',
                                  ingress_firewall_policy_id=fwp_id,
                                  egress_firewall_policy_id=fwp_id,
                                  description='fwg') as fwg3:
                fwgrps = [fwg1, fwg2, fwg3]
                self._test_list_resources('firewall_group', fwgrps,
                                          query_params='description=fwg')

    def test_update_firewall_group(self):
        name = "new_firewall1"
        attrs = self._get_test_firewall_group_attrs(name)

        with self.firewall_policy() as fwp:
            fwp_id = fwp['firewall_policy']['id']
            with self.firewall_group(
                    ingress_firewall_policy_id=fwp_id,
                    admin_state_up=ADMIN_STATE_UP) as firewall:
                data = {'firewall_group': {'name': name}}
                req = self.new_update_request('firewall_groups', data,
                                              firewall['firewall_group']['id'])
                res = self.deserialize(self.fmt,
                                       req.get_response(self.ext_api))
                for k, v in six.iteritems(attrs):
                    self.assertEqual(v, res['firewall_group'][k])

    def test_update_firewall_group_with_fwp(self):
        ctx = context.Context('not_admin', 'tenant1')
        with self.firewall_policy(
                name='p1', tenant_id='tenant1', public=False) as fwp1, \
                self.firewall_policy(
                    name='p2', tenant_id='tenant1', public=False) as fwp2, \
                self.firewall_group(
                    ingress_firewall_policy_id=fwp1['firewall_policy']['id'],
                    egress_firewall_policy_id=fwp2['firewall_policy']['id'],
                    context=ctx) as fw:
            fw_id = fw['firewall_group']['id']
            fwp2_id = fwp2['firewall_policy']['id']
            data = {'firewall_group': {'ingress_firewall_policy_id': fwp2_id}}
            req = self.new_update_request('firewall_groups', data, fw_id,
                                          context=ctx)
            res = req.get_response(self.ext_api)
            self.assertEqual(200, res.status_int)

    @testtools.skip('bug/1614680')
    def test_update_firewall_group_with_public_fwp(self):
        ctx = context.Context('not_admin', 'tenant1')
        with self.firewall_policy(name='p1', tenant_id='tenant1', public=True) as fwp1, \
                self.firewall_policy(name='p2', tenant_id='tenant2', public=True) as fwp2, \
                self.firewall_group(
                    ingress_firewall_policy_id=fwp1['firewall_policy']['id'],
                    egress_firewall_policy_id=fwp1['firewall_policy']['id'],
                    context=ctx) as fw:
            fw_id = fw['firewall_group']['id']
            fwp2_id = fwp2['firewall_policy']['id']
            data = {'firewall_group': {'ingress_firewall_policy_id': fwp2_id}}
            req = self.new_update_request('firewall_groups', data, fw_id,
                                          context=ctx)
            res = req.get_response(self.ext_api)
            self.assertEqual(200, res.status_int)

    def test_update_firewall_group_with_admin_and_fwp_different_tenant(self):
        ctx = context.get_admin_context()
        with self.firewall_policy() as fwp1, \
                self.firewall_policy(
                    tenant_id='tenant2', public=False) as fwp2, \
                self.firewall_group(
                    ingress_firewall_policy_id=fwp1['firewall_policy']['id'],
                    egress_firewall_policy_id=fwp1['firewall_policy']['id'],
                    context=ctx) as fw:
            fw_id = fw['firewall_group']['id']
            fwp2_id = fwp2['firewall_policy']['id']
            data = {'firewall_group': {'egress_firewall_policy_id': fwp2_id}}
            req = self.new_update_request('firewall_groups', data, fw_id,
                                          context=ctx)
            res = req.get_response(self.ext_api)
            self.assertEqual(409, res.status_int)

    def test_update_firewall_group_fwp_not_found_on_different_tenant(self):
        with self.firewall_policy(name='fwp1', tenant_id='tenant1',
                                  do_delete=False) as fwp1, \
                self.firewall_policy(name='fwp2', public=False,
                                     tenant_id='tenant2') as fwp2:

            fwps = [fwp1, fwp2]
            # create firewall using fwp1 exists the same tenant.
            fwp1_id = fwps[0]['firewall_policy']['id']
            fwp2_id = fwps[1]['firewall_policy']['id']
            ctx = context.Context('not_admin', 'tenant1')
            with self.firewall_group(ingress_firewall_policy_id=fwp1_id,
                               context=ctx) as firewall:
                fw_id = firewall['firewall_group']['id']
                fw_db = self.plugin._get_firewall_group(ctx, fw_id)
                fw_db['status'] = constants.ACTIVE
                # update firewall from fwp1 to fwp2(different tenant)
                data = {'firewall_group':
                        {'ingress_firewall_policy_id': fwp2_id}}
                req = self.new_update_request('firewall_groups', data, fw_id,
                                              context=ctx)
                res = req.get_response(self.ext_api)
                self.assertEqual(404, res.status_int)

    def test_delete_firewall(self):
        ctx = context.get_admin_context()
        with self.firewall_policy() as fwp:
            fwp_id = fwp['firewall_policy']['id']
            with self.firewall_group(ingress_firewall_policy_id=fwp_id,
                               do_delete=False) as fw:
                fw_id = fw['firewall_group']['id']
                req = self.new_delete_request('firewall_groups', fw_id)
                res = req.get_response(self.ext_api)
                self.assertEqual(204, res.status_int)
                self.assertRaises(firewall.FirewallGroupNotFound,
                                  self.plugin.get_firewall_group,
                                  ctx, fw_id)

    def test_insert_rule_in_policy_with_prior_rules_added_via_update(self):
        attrs = self._get_test_firewall_policy_attrs()
        attrs['audited'] = False
        with self.firewall_rule(name='fwr1') as fwr1, \
                self.firewall_rule(name='fwr2') as fwr2, \
                self.firewall_rule(name='fwr3') as fwr3:
            frs = [fwr1, fwr2, fwr3]
            fr1 = frs[0:2]
            fwr3 = frs[2]
            with self.firewall_policy() as fwp:
                fwp_id = fwp['firewall_policy']['id']
                attrs['id'] = fwp_id
                fw_rule_ids = [r['firewall_rule']['id'] for r in fr1]
                attrs['firewall_rules'] = fw_rule_ids[:]
                data = {'firewall_policy': {'firewall_rules': fw_rule_ids}}
                req = self.new_update_request('firewall_policies', data,
                                              fwp_id)
                req.get_response(self.ext_api)
                self._rule_action('insert', fwp_id, fw_rule_ids[0],
                                  insert_before=fw_rule_ids[0],
                                  insert_after=None,
                                  expected_code=webob.exc.HTTPConflict.code,
                                  expected_body=None)
                fwr3_id = fwr3['firewall_rule']['id']
                attrs['firewall_rules'].insert(0, fwr3_id)
                self._rule_action('insert', fwp_id, fwr3_id,
                                  insert_before=fw_rule_ids[0],
                                  insert_after=None,
                                  expected_code=webob.exc.HTTPOk.code,
                                  expected_body=attrs)

    def test_insert_rule_in_policy_failures(self):
        with self.firewall_rule(name='fwr1') as fr1:
            with self.firewall_policy() as fwp:
                fwp_id = fwp['firewall_policy']['id']
                fr1_id = fr1['firewall_rule']['id']
                fw_rule_ids = [fr1_id]
                data = {'firewall_policy':
                        {'firewall_rules': fw_rule_ids}}
                req = self.new_update_request('firewall_policies', data,
                                              fwp_id)
                req.get_response(self.ext_api)
                # test inserting with empty request body
                self._rule_action('insert', fwp_id, '123',
                                  expected_code=webob.exc.HTTPBadRequest.code,
                                  expected_body=None, body_data={})
                # test inserting when firewall_rule_id is missing in
                # request body
                insert_data = {'insert_before': '123',
                               'insert_after': '456'}
                self._rule_action('insert', fwp_id, '123',
                                  expected_code=webob.exc.HTTPBadRequest.code,
                                  expected_body=None,
                                  body_data=insert_data)
                # test inserting when firewall_rule_id is None
                insert_data = {'firewall_rule_id': None,
                               'insert_before': '123',
                               'insert_after': '456'}
                self._rule_action('insert', fwp_id, '123',
                                  expected_code=webob.exc.HTTPNotFound.code,
                                  expected_body=None,
                                  body_data=insert_data)
                # test inserting when firewall_policy_id is incorrect
                self._rule_action('insert', '123', fr1_id,
                                  expected_code=webob.exc.HTTPNotFound.code,
                                  expected_body=None)
                # test inserting when firewall_policy_id is None
                self._rule_action('insert', None, fr1_id,
                                  expected_code=webob.exc.HTTPBadRequest.code,
                                  expected_body=None)

    def test_insert_rule_for_previously_associated_rule(self):
        with self.firewall_rule() as fwr:
            fwr_id = fwr['firewall_rule']['id']
            fw_rule_ids = [fwr_id]
            with self.firewall_policy(firewall_rules=fw_rule_ids):
                with self.firewall_policy(name='firewall_policy2') as fwp:
                    fwp_id = fwp['firewall_policy']['id']
                    insert_data = {'firewall_rule_id': fwr_id}
                    self._rule_action(
                        'insert', fwp_id, fwr_id, insert_before=None,
                        insert_after=None,
                        expected_code=webob.exc.HTTPOk.code,
                        expected_body=None, body_data=insert_data)

    def test_insert_rule_for_previously_associated_rule_other_tenant(self):
        with self.firewall_rule(tenant_id='tenant-2') as fwr:
            fwr_id = fwr['firewall_rule']['id']
            fw_rule_ids = [fwr_id]
            with self.firewall_policy(tenant_id='tenant-2',
                                      firewall_rules=fw_rule_ids):
                with self.firewall_policy(name='firewall_policy2') as fwp:
                    fwp_id = fwp['firewall_policy']['id']
                    insert_data = {'firewall_rule_id': fwr_id}
                    self._rule_action(
                        'insert', fwp_id, fwr_id, insert_before=None,
                        insert_after=None,
                        expected_code=webob.exc.HTTPOk.code,
                        expected_body=None, body_data=insert_data)

    def test_insert_rule_for_prev_associated_ref_rule(self):
        with self.firewall_rule(name='fwr0') as fwr0, \
                self.firewall_rule(name='fwr1') as fwr1:
            fwr = [fwr0, fwr1]
            fwr0_id = fwr[0]['firewall_rule']['id']
            fwr1_id = fwr[1]['firewall_rule']['id']
            with self.firewall_policy(name='fwp0') as fwp0, \
                    self.firewall_policy(name='fwp1',
                                         firewall_rules=[fwr1_id]) as fwp1:
                fwp = [fwp0, fwp1]
                fwp0_id = fwp[0]['firewall_policy']['id']
                # test inserting before a rule which
                # is associated with different policy
                self._rule_action('insert', fwp0_id, fwr0_id,
                                  insert_before=fwr1_id,
                                  expected_code=webob.exc.HTTPBadRequest.code,
                                  expected_body=None)
                # test inserting  after a rule which
                # is associated with different policy
                self._rule_action('insert', fwp0_id, fwr0_id,
                                  insert_after=fwr1_id,
                                  expected_code=webob.exc.HTTPBadRequest.code,
                                  expected_body=None)

    def test_insert_rule_for_policy_of_other_tenant(self):
        with self.firewall_rule(tenant_id='tenant-2', public=False) as fwr:
            fwr_id = fwr['firewall_rule']['id']
            with self.firewall_policy(name='firewall_policy') as fwp:
                fwp_id = fwp['firewall_policy']['id']
                insert_data = {'firewall_rule_id': fwr_id}
                self._rule_action(
                    'insert', fwp_id, fwr_id, insert_before=None,
                    insert_after=None,
                    expected_code=webob.exc.HTTPConflict.code,
                    expected_body=None, body_data=insert_data)

    def test_insert_rule_in_policy(self):
        attrs = self._get_test_firewall_policy_attrs()
        attrs['audited'] = False
        with self.firewall_rule(name='fwr0') as fwr0, \
                self.firewall_rule(name='fwr1') as fwr1, \
                self.firewall_rule(name='fwr2') as fwr2, \
                self.firewall_rule(name='fwr3') as fwr3, \
                self.firewall_rule(name='fwr4') as fwr4, \
                self.firewall_rule(name='fwr5') as fwr5, \
                self.firewall_rule(name='fwr6') as fwr6:
            fwr = [fwr0, fwr1, fwr2, fwr3, fwr4, fwr5, fwr6]
            with self.firewall_policy() as fwp:
                fwp_id = fwp['firewall_policy']['id']
                attrs['id'] = fwp_id
                # test insert when rule list is empty
                fwr0_id = fwr[0]['firewall_rule']['id']
                attrs['firewall_rules'].insert(0, fwr0_id)
                self._rule_action('insert', fwp_id, fwr0_id,
                                  insert_before=None,
                                  insert_after=None,
                                  expected_code=webob.exc.HTTPOk.code,
                                  expected_body=attrs)
                # test insert at top of rule list, insert_before and
                # insert_after not provided
                fwr1_id = fwr[1]['firewall_rule']['id']
                attrs['firewall_rules'].insert(0, fwr1_id)
                insert_data = {'firewall_rule_id': fwr1_id}
                self._rule_action('insert', fwp_id, fwr0_id,
                                  expected_code=webob.exc.HTTPOk.code,
                                  expected_body=attrs, body_data=insert_data)
                # test insert at top of list above existing rule
                fwr2_id = fwr[2]['firewall_rule']['id']
                attrs['firewall_rules'].insert(0, fwr2_id)
                self._rule_action('insert', fwp_id, fwr2_id,
                                  insert_before=fwr1_id,
                                  insert_after=None,
                                  expected_code=webob.exc.HTTPOk.code,
                                  expected_body=attrs)
                # test insert at bottom of list
                fwr3_id = fwr[3]['firewall_rule']['id']
                attrs['firewall_rules'].append(fwr3_id)
                self._rule_action('insert', fwp_id, fwr3_id,
                                  insert_before=None,
                                  insert_after=fwr0_id,
                                  expected_code=webob.exc.HTTPOk.code,
                                  expected_body=attrs)
                # test insert in the middle of the list using
                # insert_before
                fwr4_id = fwr[4]['firewall_rule']['id']
                attrs['firewall_rules'].insert(1, fwr4_id)
                self._rule_action('insert', fwp_id, fwr4_id,
                                  insert_before=fwr1_id,
                                  insert_after=None,
                                  expected_code=webob.exc.HTTPOk.code,
                                  expected_body=attrs)
                # test insert in the middle of the list using
                # insert_after
                fwr5_id = fwr[5]['firewall_rule']['id']
                attrs['firewall_rules'].insert(1, fwr5_id)
                self._rule_action('insert', fwp_id, fwr5_id,
                                  insert_before=None,
                                  insert_after=fwr2_id,
                                  expected_code=webob.exc.HTTPOk.code,
                                  expected_body=attrs)
                # test insert when both insert_before and
                # insert_after are set
                fwr6_id = fwr[6]['firewall_rule']['id']
                attrs['firewall_rules'].insert(1, fwr6_id)
                self._rule_action('insert', fwp_id, fwr6_id,
                                  insert_before=fwr5_id,
                                  insert_after=fwr5_id,
                                  expected_code=webob.exc.HTTPOk.code,
                                  expected_body=attrs)

    def test_remove_rule_from_policy(self):
        attrs = self._get_test_firewall_policy_attrs()
        attrs['audited'] = False
        with self.firewall_rule(name='fwr1') as fwr1, \
                self.firewall_rule(name='fwr2') as fwr2, \
                self.firewall_rule(name='fwr3') as fwr3:
            fr1 = [fwr1, fwr2, fwr3]
            with self.firewall_policy() as fwp:
                fwp_id = fwp['firewall_policy']['id']
                attrs['id'] = fwp_id
                fw_rule_ids = [r['firewall_rule']['id'] for r in fr1]
                attrs['firewall_rules'] = fw_rule_ids[:]
                data = {'firewall_policy':
                        {'firewall_rules': fw_rule_ids}}
                req = self.new_update_request('firewall_policies', data,
                                              fwp_id)
                req.get_response(self.ext_api)
                # test removing a rule from a policy that does not exist
                self._rule_action('remove', '123', fw_rule_ids[1],
                                  expected_code=webob.exc.HTTPNotFound.code,
                                  expected_body=None)
                # test removing a rule in the middle of the list
                attrs['firewall_rules'].remove(fw_rule_ids[1])
                self._rule_action('remove', fwp_id, fw_rule_ids[1],
                                  expected_body=attrs)
                # test removing a rule at the top of the list
                attrs['firewall_rules'].remove(fw_rule_ids[0])
                self._rule_action('remove', fwp_id, fw_rule_ids[0],
                                  expected_body=attrs)
                # test removing remaining rule in the list
                attrs['firewall_rules'].remove(fw_rule_ids[2])
                self._rule_action('remove', fwp_id, fw_rule_ids[2],
                                  expected_body=attrs)
                # test removing rule that is not associated with the policy
                self._rule_action('remove', fwp_id, fw_rule_ids[2],
                                  expected_code=webob.exc.HTTPBadRequest.code,
                                  expected_body=None)

    def test_remove_rule_from_policy_failures(self):
        with self.firewall_rule(name='fwr1') as fr1:
            with self.firewall_policy() as fwp:
                fwp_id = fwp['firewall_policy']['id']
                fw_rule_ids = [fr1['firewall_rule']['id']]
                data = {'firewall_policy':
                        {'firewall_rules': fw_rule_ids}}
                req = self.new_update_request('firewall_policies', data,
                                              fwp_id)
                req.get_response(self.ext_api)
                # test removing rule that does not exist
                self._rule_action('remove', fwp_id, '123',
                                  expected_code=webob.exc.HTTPNotFound.code,
                                  expected_body=None)
                # test removing rule with bad request
                self._rule_action('remove', fwp_id, '123',
                                  expected_code=webob.exc.HTTPBadRequest.code,
                                  expected_body=None, body_data={})
                # test removing rule with firewall_rule_id set to None
                self._rule_action('remove', fwp_id, '123',
                                  expected_code=webob.exc.HTTPNotFound.code,
                                  expected_body=None,
                                  body_data={'firewall_rule_id': None})

    def test_show_firewall_rule_by_name(self):
        with self.firewall_rule(name='firewall_Rule1') as fw_rule:
            res = self._show('firewall_rules',
                             fw_rule['firewall_rule']['id'])
            self.assertEqual('firewall_Rule1', res['firewall_rule']['name'])

    def test_show_firewall_policy_by_name(self):
        with self.firewall_policy(
            name='firewall_Policy1') as fw_policy:
            res = self._show('firewall_policies',
                             fw_policy['firewall_policy']['id'])
            self.assertEqual(
                'firewall_Policy1', res['firewall_policy']['name'])

    def test_show_firewall_group_by_name(self):
        with self.firewall_group(name='fireWall1') as fw:
            res = self._show('firewall_groups', fw['firewall_group']['id'])
            self.assertEqual('fireWall1', res['firewall_group']['name'])
