# Copyright 2014 OpenStack Foundation.
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

from neutron.openstack.common import log as logging
from neutron.services import advanced_service
from neutron.services import provider_configuration as provconf
from oslo.config import cfg
from oslo.utils import importutils

LOG = logging.getLogger(__name__)

FIREWALL_DRIVERS = 'firewall_drivers'


class FirewallService(advanced_service.AdvancedService):
    """Firewall Service observer."""

    def load_device_drivers(self):
        """Loads a single device driver for FWaaS."""
        device_driver = provconf.get_provider_driver_class(
            cfg.CONF.fwaas.driver, FIREWALL_DRIVERS)
        try:
            self.devices = importutils.import_object(device_driver)
            LOG.debug('Loaded FWaaS device driver: %s', device_driver)
            return self.devices
        except ImportError:
            msg = _('Error importing FWaaS device driver: %s')
            raise ImportError(msg % device_driver)
        except ValueError:
            msg = _('Configuration error - no FWaaS device_driver specified')
            raise ValueError(msg)
