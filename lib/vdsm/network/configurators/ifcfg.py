# Copyright 2011-2017 Red Hat, Inc.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA
#
# Refer to the README and COPYING files for full details of the license
#
from __future__ import absolute_import

from contextlib import contextmanager
import copy
import errno
import glob
import logging
import os
import pipes
import pwd
import re
import selinux
import shutil

import six

from vdsm.config import config
from vdsm import constants
from vdsm import dsaversion
from vdsm import hooks
from vdsm.common import concurrent
from vdsm.common import fileutils
from vdsm.common.conv import tobool

from vdsm.network import cmd
from vdsm.network import ifacetracking
from vdsm.network import ipwrapper
from vdsm.network import sysctl
from vdsm.network.ip import address
from vdsm.network.ip import dhclient
from vdsm.network.link import iface as link_iface
from vdsm.network.link.bond import Bond
from vdsm.network.link.bond.sysfs_driver import BONDING_MASTERS
from vdsm.network.link.bond.sysfs_options import BONDING_MODES_NAME_TO_NUMBER
from vdsm.network.link.setup import parse_bond_options
from vdsm.network.link.setup import remove_custom_bond_option
from vdsm.network.netconfpersistence import RunningConfig, PersistentConfig
from vdsm.network.netinfo import mtus, nics, vlans, misc, NET_PATH
from vdsm.network.netlink import waitfor

from . import Configurator, getEthtoolOpts
from .ifcfg_acquire import IfcfgAcquire
from ..errors import ConfigNetworkError, ERR_BAD_BONDING, ERR_FAILED_IFUP
from ..models import Nic, Bridge, Bond as bond_model
from ..sourceroute import StaticSourceRoute

NET_CONF_DIR = '/etc/sysconfig/network-scripts/'
NET_CONF_BACK_DIR = constants.P_VDSM_LIB + 'netconfback/'
NET_CONF_PREF = NET_CONF_DIR + 'ifcfg-'

CONFFILE_HEADER_SIGNATURE = '# Generated by VDSM version'


def is_available():
    return True


class Ifcfg(Configurator):
    # TODO: Do all the configApplier interaction from here.
    def __init__(self, net_info, inRollback=False):
        is_unipersistence = config.get('vars', 'net_persistence') == 'unified'
        super(Ifcfg, self).__init__(ConfigWriter(),
                                    net_info,
                                    is_unipersistence,
                                    inRollback)
        self.runningConfig = RunningConfig()

    def rollback(self):
        """This reimplementation always returns None since Ifcfg can rollback
        on its own via restoreBackups(). This makes the general mechanism of
        API.Global._rollback redundant in this case."""
        self.configApplier.restoreBackups()
        self.configApplier = None
        self.runningConfig = None

    def commit(self):
        self.configApplier = None
        self.runningConfig.save()
        self.runningConfig = None

    def configureBridge(self, bridge, **opts):
        if not self.owned_device(bridge.name):
            IfcfgAcquire.acquire_device(bridge.name)
        self.configApplier.addBridge(bridge, **opts)
        ifdown(bridge.name)
        if bridge.port:
            bridge.port.configure(**opts)
        self._addSourceRoute(bridge)
        _ifup(bridge)

    def configureVlan(self, vlan, **opts):
        if not self.owned_device(vlan.name):
            IfcfgAcquire.acquire_device(vlan.name)
            IfcfgAcquire.acquire_vlan_device(vlan.name)
        self.configApplier.addVlan(vlan, **opts)
        vlan.device.configure(**opts)
        self._addSourceRoute(vlan)
        if isinstance(vlan.device, bond_model):
            Ifcfg._ifup_vlan_with_slave_bond_hwaddr_sync(vlan)
        else:
            _ifup(vlan)

    def configureBond(self, bond, **opts):
        if not self.owned_device(bond.name):
            IfcfgAcquire.acquire_device(bond.name)
        self.configApplier.addBonding(bond, self.net_info, **opts)
        if not vlans.is_vlanned(bond.name):
            for slave in bond.slaves:
                ifdown(slave.name)
        for slave in bond.slaves:
            slave.configure(**opts)
        self._addSourceRoute(bond)
        _ifup(bond)

        # When acquiring the device from NM, it may take a few seconds until
        # the bond is released by NM and loaded through initscripts.
        # Giving it a chance to come up before continuing.
        with waitfor.waitfor_linkup(bond.name):
            pass

        self.runningConfig.setBonding(
            bond.name, {'options': bond.options,
                        'nics': sorted(s.name for s in bond.slaves),
                        'switch': 'legacy'})

    def editBonding(self, bond, _netinfo):
        """
        Modifies the bond so that the bond in the system ends up with the
        same slave and options configuration that are requested. Makes a
        best effort not to interrupt connectivity.
        """
        nicsToSet = frozenset(nic.name for nic in bond.slaves)
        currentNics = frozenset(_netinfo.getNicsForBonding(bond.name))
        nicsToAdd = nicsToSet - currentNics

        if not self.owned_device(bond.name):
            IfcfgAcquire.acquire_device(bond.name)

        # Create bond configuration in case it was a non ifcfg controlled bond.
        # Needed to be before slave configuration for initscripts to add slave
        # to bond.
        bondIfcfgWritten = False
        isIfcfgControlled = os.path.isfile(NET_CONF_PREF + bond.name)
        areOptionsApplied = bond.areOptionsApplied()
        if not isIfcfgControlled or not areOptionsApplied:
            bridgeName = _netinfo.getBridgedNetworkForIface(bond.name)
            if isIfcfgControlled and bridgeName:
                bond.master = Bridge(bridgeName, self, port=bond)
            self.configApplier.addBonding(bond, _netinfo)
            bondIfcfgWritten = True

        for nic in currentNics - nicsToSet:
            ifdown(nic)  # So that no users will be detected for it.
            Nic(nic, self, _netinfo=_netinfo).remove()

        for slave in bond.slaves:
            if slave.name in nicsToAdd:
                ifdown(slave.name)  # nics must be down to join a bond
                self.configApplier.addNic(slave, _netinfo)
                _exec_ifup(slave)
            else:
                # Let NetworkManager know that we now own the slave.
                self.configApplier.addNic(slave, _netinfo)

        if bondIfcfgWritten:
            ifdown(bond.name)
            _restore_default_bond_options(bond.name, bond.options)
            _exec_ifup(bond)
        self.runningConfig.setBonding(
            bond.name, {'options': bond.options,
                        'nics': sorted(slave.name for slave in bond.slaves),
                        'switch': 'legacy'})

    def configureNic(self, nic, **opts):
        if not self.owned_device(nic.name):
            IfcfgAcquire.acquire_device(nic.name)
        self.configApplier.addNic(nic, self.net_info, **opts)
        self._addSourceRoute(nic)
        if nic.bond is None:
            if not vlans.is_vlanned(nic.name):
                ifdown(nic.name)
            _ifup(nic)

    def removeBridge(self, bridge):
        if not self.owned_device(bridge.name):
            IfcfgAcquire.acquire_device(bridge.name)
        if bridge.ipv4.bootproto == 'dhcp':
            ifacetracking.add(bridge.name)
        ifdown(bridge.name)
        self._removeSourceRoute(bridge)
        cmd.exec_sync([constants.EXT_BRCTL, 'delbr', bridge.name])
        self.configApplier.removeBridge(bridge.name)
        self.net_info.del_bridge(bridge.name)
        if bridge.port:
            bridge.port.remove()

    def removeVlan(self, vlan):
        if not self.owned_device(vlan.name):
            IfcfgAcquire.acquire_device(vlan.name)
            IfcfgAcquire.acquire_vlan_device(vlan.name)
        if vlan.ipv4.bootproto == 'dhcp':
            ifacetracking.add(vlan.name)
        ifdown(vlan.name)
        self._removeSourceRoute(vlan)
        self.configApplier.removeVlan(vlan.name)
        self.net_info.del_vlan(vlan.name)
        vlan.device.remove()

    def _ifaceDownAndCleanup(self, iface, remove_even_if_used=False):
        """Returns True iff the iface is to be removed."""
        if iface.ipv4.bootproto == 'dhcp':
            ifacetracking.add(iface.name)
        to_be_removed = remove_even_if_used or not self.net_info.ifaceUsers(
            iface.name)
        if to_be_removed:
            ifdown(iface.name)
        self._removeSourceRoute(iface)
        return to_be_removed

    def _addSourceRoute(self, netEnt):
        """For ifcfg tracking can be done together with route/rule addition"""
        ipv4 = netEnt.ipv4
        if ipv4.bootproto != 'dhcp' and netEnt.master is None:
            valid_args = (ipv4.address and ipv4.netmask and
                          ipv4.gateway not in (None, '0.0.0.0'))
            if valid_args:
                sroute = StaticSourceRoute(netEnt.name, ipv4.address,
                                           ipv4.netmask, ipv4.gateway)
                self.configureSourceRoute(*sroute.requested_config())

            else:
                logging.warning(
                    'Invalid input for source routing: '
                    'name=%s, addr=%s, netmask=%s, gateway=%s',
                    netEnt.name, ipv4.address, ipv4.netmask, ipv4.gateway)

        if netEnt.ipv4.bootproto == 'dhcp':
            ifacetracking.add(netEnt.name)

    def _removeSourceRoute(self, netEnt):
        if netEnt.ipv4.bootproto != 'dhcp' and netEnt.master is None:
            logging.debug("Removing source route for device %s", netEnt.name)
            sroute = StaticSourceRoute(netEnt.name, None, None, None)
            self.removeSourceRoute(*sroute.current_config())

    def removeBond(self, bonding):
        if not self.owned_device(bonding.name):
            IfcfgAcquire.acquire_device(bonding.name)
        to_be_removed = self._ifaceDownAndCleanup(bonding)
        if to_be_removed:
            self.configApplier.removeBonding(bonding.name)
            if bonding.on_removal_just_detach_from_network:
                # Recreate the bond with ip and master info cleared
                bonding.ipv4 = address.IPv4()
                bonding.ipv6 = address.IPv6()
                bonding.master = None
                bonding.configure()
            else:
                for slave in bonding.slaves:
                    slave.remove()
                self.runningConfig.removeBonding(bonding.name)
        else:
            set_mtu = self._setNewMtu(bonding,
                                      vlans.vlan_devs_for_iface(bonding.name))
            # Since we are not taking the device up again, ifcfg will not be
            # read at this point and we need to set the live mtu value.
            # Note that ip link set dev bondX mtu Y sets Y on all its links
            if set_mtu is not None:
                ipwrapper.linkSet(bonding.name, ['mtu', str(set_mtu)])

            # If the bond was bridged, we must remove BRIDGE parameter from its
            # ifcfg configuration file.
            if bonding.bridge:
                self.configApplier.dropBridgeParameter(bonding.name)

    def removeNic(self, nic, remove_even_if_used=False):
        if not self.owned_device(nic.name):
            IfcfgAcquire.acquire_device(nic.name)
        to_be_removed = self._ifaceDownAndCleanup(nic, remove_even_if_used)
        if to_be_removed:
            self.configApplier.removeNic(nic.name)
            if nic.name in nics.nics():
                _exec_ifup(nic)
            else:
                logging.warning('host interface %s missing', nic.name)
        else:
            set_mtu = self._setNewMtu(nic, vlans.vlan_devs_for_iface(nic.name))
            # Since we are not taking the device up again, ifcfg will not be
            # read at this point and we need to set the live mtu value
            if set_mtu is not None:
                ipwrapper.linkSet(nic.name, ['mtu', str(set_mtu)])

            # If the nic was bridged, we must remove BRIDGE parameter from its
            # ifcfg configuration file.
            if nic.bridge:
                self.configApplier.dropBridgeParameter(nic.name)

    def _getFilePath(self, fileType, device):
        return os.path.join(NET_CONF_DIR, '%s-%s' % (fileType, device))

    def _removeSourceRouteFile(self, fileType, device):
        filePath = self._getFilePath(fileType, device)
        self.configApplier._backup(filePath)
        self.configApplier._removeFile(filePath)

    def _writeConfFile(self, contents, fileType, device):
        filePath = self._getFilePath(fileType, device)

        configuration = ''
        for entry in contents:
            configuration += str(entry) + '\n'

        self.configApplier.writeConfFile(filePath, configuration)

    def configureSourceRoute(self, routes, rules, device):
        self._writeConfFile(routes, 'route', device)
        self._writeConfFile(rules, 'rule', device)

    def removeSourceRoute(self, routes, rules, device):
        self._removeSourceRouteFile('rule', device)
        self._removeSourceRouteFile('route', device)

    @staticmethod
    def owned_device(device):
        try:
            with open(misc.NET_CONF_PREF + device) as conf:
                content = conf.read()
        except IOError as ioe:
            if ioe.errno == errno.ENOENT:
                return False
            else:
                raise
        else:
            return content.startswith(CONFFILE_HEADER_SIGNATURE)

    @staticmethod
    def _ifup_vlan_with_slave_bond_hwaddr_sync(vlan):
        """
        When NM is active and the network includes a vlan on top of a bond, the
        following scenario may occur:
        - VLAN over a bond with slaves is already defined in the system and
          VDSM is about to acquire it to define on it a network.
        - The VLAN iface is re-created while the bond slaves are temporary
          detached, causing the vlan to be created with the bond temporary
          mac address, which is different from the original existing address.
        Therefore, following the VLAN ifup command, its mac address is compared
        with the first bond slave. In case they differ, the vlan device is
        recreated.
        """
        bond = vlan.device
        if not bond.slaves:
            _ifup(vlan)
            return

        for attempt in range(5):
            _ifup(vlan)
            if (link_iface.mac_address(bond.slaves[0].name) ==
                    link_iface.mac_address(vlan.name)):
                return

            logging.info('Config vlan@bond: hwaddr not in sync (%s)', attempt)
            ifdown(vlan.name)

        raise ConfigNetworkError(
            ERR_BAD_BONDING,
            'While adding vlan {} over bond {}, '
            'the bond hwaddr was not in sync '
            'whith its slaves.'.format(vlan.name, vlan.device.name))


class ConfigWriter(object):
    CONFFILE_HEADER = (CONFFILE_HEADER_SIGNATURE + ' ' +
                       dsaversion.raw_version_revision)
    DELETED_HEADER = '# original file did not exist'

    def __init__(self):
        self._backups = {}

    @staticmethod
    def _removeFile(filename):
        fileutils.rm_file(filename)
        logging.debug("Removed file %s", filename)

    @classmethod
    def writeBackupFile(cls, dirName, fileName, content):
        backup = os.path.join(dirName, fileName)
        if os.path.exists(backup):
            # original copy already backed up
            return

        vdsm_uid = pwd.getpwnam('vdsm').pw_uid

        # make directory (if it doesn't exist) and assign it to vdsm
        if not os.path.exists(dirName):
            os.makedirs(dirName)
        os.chown(dirName, vdsm_uid, -1)

        with open(backup, 'w') as backupFile:
            backupFile.write(content)
        os.chown(backup, vdsm_uid, -1)
        logging.debug("Persistently backed up %s "
                      "(until next 'set safe config')", backup)

    def _backup(self, filename):
        self._atomicBackup(filename)
        if filename not in _get_unified_persistence_ifcfg():
            self._persistentBackup(filename)

    def _atomicBackup(self, filename):
        """
        Backs up configuration to memory,
        for a later rollback in case of error.
        """

        if filename not in self._backups:
            try:
                with open(filename) as f:
                    self._backups[filename] = f.read()
                logging.debug("Backed up %s", filename)
            except IOError as e:
                if e.errno == os.errno.ENOENT:
                    self._backups[filename] = None
                else:
                    raise

    def restoreAtomicBackup(self):
        logging.info("Rolling back configuration (restoring atomic backup)")
        for confFilePath, content in six.iteritems(self._backups):
            if content is None:
                logging.debug('Removing empty configuration backup %s',
                              confFilePath)
                self._removeFile(confFilePath)
            else:
                with open(confFilePath, 'w') as confFile:
                    confFile.write(content)
            logging.info('Restored %s', confFilePath)

    @classmethod
    def _persistentBackup(cls, filename):
        """ Persistently backup ifcfg-* config files """
        if os.path.exists('/usr/libexec/ovirt-functions'):
            cmd.exec_sync([constants.EXT_SH, '/usr/libexec/ovirt-functions',
                           'unmount_config', filename])
            logging.debug("unmounted %s using ovirt", filename)

        (dummy, basename) = os.path.split(filename)
        try:
            with open(filename) as f:
                content = f.read()
        except IOError as e:
            if e.errno == os.errno.ENOENT:
                # For non-exists ifcfg-* file use predefined header
                content = cls.DELETED_HEADER + '\n'
            else:
                raise
        logging.debug("backing up %s: %s", basename, content)

        cls.writeBackupFile(NET_CONF_BACK_DIR, basename, content)

    def restorePersistentBackup(self):
        """Restore network config to last known 'safe' state"""

        self.loadBackups()
        self.restoreBackups()
        self.clearBackups()

    def _loadBackupFiles(self, loadDir, restoreDir):
        for fpath in glob.iglob(loadDir + '/*'):
            if not os.path.isfile(fpath):
                continue

            with open(fpath) as f:
                content = f.read()
            if content.startswith(self.DELETED_HEADER):
                content = None

            basename = os.path.basename(fpath)
            self._backups[os.path.join(restoreDir, basename)] = content

            logging.info('Loaded %s', fpath)

    def loadBackups(self):
        """ Load persistent backups into memory """
        self._loadBackupFiles(NET_CONF_BACK_DIR, NET_CONF_DIR)

    def restoreBackups(self):
        """ Restore network backups from memory."""
        if not self._backups:
            return

        stop_devices(self._backups)

        self.restoreAtomicBackup()

        start_devices(self._backups)

    @staticmethod
    def clearBackups():
        """ Clear backup files """
        for fpath in glob.iglob(NET_CONF_BACK_DIR + "*"):
            if os.path.isdir(fpath):
                shutil.rmtree(fpath)
            else:
                os.remove(fpath)

    def writeConfFile(self, fileName, configuration):
        '''Backs up the previous contents of the file referenced by fileName
        writes the new configuration and sets the specified access mode.'''
        self._backup(fileName)
        configuration = self.CONFFILE_HEADER + '\n' + configuration

        logging.debug('Writing to file %s configuration:\n%s', fileName,
                      configuration)
        with open(fileName, 'w') as confFile:
            confFile.write(configuration)
        os.chmod(fileName, 0o664)

        try:
            # filname can be of 'unicode' type. restorecon calls into a C API
            # that needs a char *. Thus, it is necessary to encode unicode to
            # a utf-8 string.
            selinux.restorecon(fileName.encode('utf-8'))
        except:
            logging.debug('ignoring restorecon error in case '
                          'SElinux is disabled', exc_info=True)

    def _createConfFile(self, conf, name, ipv4, ipv6, mtu, nameservers,
                        **kwargs):
        """ Create ifcfg-* file with proper fields per device """
        cfg = 'DEVICE=%s\n' % pipes.quote(name)
        cfg += conf
        if ipv4.address:
            cfg += 'IPADDR=%s\n' % pipes.quote(ipv4.address)
            cfg += 'NETMASK=%s\n' % pipes.quote(ipv4.netmask)
            if ipv4.defaultRoute and ipv4.gateway:
                cfg += 'GATEWAY=%s\n' % pipes.quote(ipv4.gateway)
            # According to manual the BOOTPROTO=none should be set
            # for static IP
            cfg += 'BOOTPROTO=none\n'
        elif ipv4.bootproto:
            cfg += 'BOOTPROTO=%s\n' % pipes.quote(ipv4.bootproto)

        # FIXME: Move this out, it is unrelated to a conf file creation.
        if os.path.exists(os.path.join(NET_PATH, name)):
            # Ask dhclient to stop any dhclient running for the device
            dhclient.kill(name)
            address.flush(name)

        if mtu:
            cfg += 'MTU=%d\n' % mtu
        is_default_route = ipv4.defaultRoute and (ipv4.gateway or
                                                  ipv4.bootproto == 'dhcp')
        cfg += 'DEFROUTE=%s\n' % _to_ifcfg_bool(is_default_route)
        cfg += 'NM_CONTROLLED=no\n'
        enable_ipv6 = ipv6.address or ipv6.ipv6autoconf or ipv6.dhcpv6
        cfg += 'IPV6INIT=%s\n' % _to_ifcfg_bool(enable_ipv6)
        if enable_ipv6:
            if ipv6.address is not None:
                cfg += 'IPV6ADDR=%s\n' % pipes.quote(ipv6.address)
                if ipv6.gateway is not None:
                    cfg += 'IPV6_DEFAULTGW=%s\n' % pipes.quote(ipv6.gateway)
            elif ipv6.dhcpv6:
                cfg += 'DHCPV6C=yes\n'
            cfg += 'IPV6_AUTOCONF=%s\n' % _to_ifcfg_bool(ipv6.ipv6autoconf)
        if nameservers:
            for i, nameserver in enumerate(nameservers[0:2], 1):
                cfg += 'DNS{}={}\n'.format(i, nameserver)

        ifcfg_file = NET_CONF_PREF + name
        hook_dict = {'ifcfg_file': ifcfg_file,
                     'config': cfg}
        hook_return = hooks.before_ifcfg_write(hook_dict)
        cfg = hook_return['config']
        self.writeConfFile(ifcfg_file, cfg)
        hooks.after_ifcfg_write(hook_return)

    def addBridge(self, bridge, **opts):
        """ Create ifcfg-* file with proper fields for bridge """
        conf = 'TYPE=Bridge\nDELAY=0\n'
        opts['hotplug'] = 'no'  # So that udev doesn't trigger an ifup
        if bridge.stp is not None:
            conf += 'STP=%s\n' % ('on' if bridge.stp else 'off')
        conf += 'ONBOOT=yes\n'
        if bridge.duid_source and dhclient.supports_duid_file():
            duid_source_file = dhclient.LEASE_FILE.format(
                '', bridge.duid_source)
            conf += 'DHCLIENTARGS="-df %s"\n' % duid_source_file

        if 'custom' in opts and 'bridge_opts' in opts['custom']:
            conf += 'BRIDGING_OPTS="%s"\n' % opts['custom']['bridge_opts']

        self._createConfFile(conf, bridge.name, bridge.ipv4, bridge.ipv6,
                             bridge.mtu, bridge.nameservers, **opts)

    def addVlan(self, vlan, **opts):
        """ Create ifcfg-* file with proper fields for VLAN """
        conf = 'VLAN=yes\n'
        opts['hotplug'] = 'no'  # So that udev doesn't trigger an ifup
        if vlan.bridge:
            conf += 'BRIDGE=%s\n' % pipes.quote(vlan.bridge.name)
        conf += 'ONBOOT=yes\n'
        self._createConfFile(conf, vlan.name, vlan.ipv4, vlan.ipv6, vlan.mtu,
                             vlan.nameservers, **opts)

    def addBonding(self, bond, net_info, **opts):
        """ Create ifcfg-* file with proper fields for bond """
        # 'custom' is not a real bond option, it just piggybacks custom values
        options = remove_custom_bond_option(bond.options)
        conf = 'BONDING_OPTS=%s\n' % pipes.quote(options)
        opts['hotplug'] = 'no'  # So that udev doesn't trigger an ifup
        if bond.bridge:
            conf += 'BRIDGE=%s\n' % pipes.quote(bond.bridge.name)
        conf += 'ONBOOT=yes\n'

        ipv4, ipv6, mtu, nameservers = self._getIfaceConfValues(bond, net_info)
        self._createConfFile(conf, bond.name, ipv4, ipv6, mtu, nameservers,
                             **opts)

        # create the bonding device to avoid initscripts noise
        with open(BONDING_MASTERS) as info:
            names = info.read().split()
        if bond.name not in names:
            with open(BONDING_MASTERS, 'w') as bondingMasters:
                bondingMasters.write('+%s\n' % bond.name)

    def addNic(self, nic, net_info, **opts):
        """ Create ifcfg-* file with proper fields for NIC """
        conf = ''
        if ipwrapper.Link._detectType(nic.name) == 'dummy':
            opts['hotplug'] = 'no'

        if nic.bridge:
            conf += 'BRIDGE=%s\n' % pipes.quote(nic.bridge.name)
        if nic.bond:
            conf += 'MASTER=%s\nSLAVE=yes\n' % pipes.quote(nic.bond.name)
        conf += 'ONBOOT=yes\n'

        ethtool_opts = getEthtoolOpts(nic.name)
        if ethtool_opts:
            conf += 'ETHTOOL_OPTS=%s\n' % pipes.quote(ethtool_opts)

        ipv4, ipv6, mtu, nameservers = self._getIfaceConfValues(nic, net_info)
        self._createConfFile(conf, nic.name, ipv4, ipv6, mtu, nameservers,
                             **opts)

    @staticmethod
    def _getIfaceConfValues(iface, net_info):
        ipv4 = copy.deepcopy(iface.ipv4)
        ipv6 = copy.deepcopy(iface.ipv6)
        mtu = iface.mtu
        nameservers = iface.nameservers
        if net_info.ifaceUsers(iface.name):
            confParams = misc.getIfaceCfg(iface.name)
            if not ipv4.address and ipv4.bootproto != 'dhcp':
                ipv4.address = confParams.get('IPADDR')
                ipv4.netmask = confParams.get('NETMASK')
                ipv4.gateway = confParams.get('GATEWAY')
                if not ipv4.bootproto:
                    ipv4.bootproto = confParams.get('BOOTPROTO')
            if ipv4.defaultRoute is None and confParams.get('DEFROUTE'):
                ipv4.defaultRoute = _from_ifcfg_bool(confParams['DEFROUTE'])
            if confParams.get('IPV6INIT') == 'yes':
                ipv6.address = confParams.get('IPV6ADDR')
                ipv6.gateway = confParams.get('IPV6_DEFAULTGW')
                ipv6.ipv6autoconf = confParams.get('IPV6_AUTOCONF') == 'yes'
                ipv6.dhcpv6 = confParams.get('DHCPV6C') == 'yes'
            if not iface.mtu:
                mtu = confParams.get('MTU')
                if mtu:
                    mtu = int(mtu)
            if iface.nameservers is None:
                nameservers = [confParams[key] for key in ('DNS1', 'DNS2')
                               if key in confParams]
        return ipv4, ipv6, mtu, nameservers

    def removeNic(self, nic):
        cf = NET_CONF_PREF + nic
        self._backup(cf)
        l = [self.CONFFILE_HEADER + '\n', 'DEVICE=%s\n' % nic, 'ONBOOT=yes\n',
             'MTU=%s\n' % mtus.DEFAULT_MTU, 'NM_CONTROLLED=no\n']
        with open(cf, 'w') as nicFile:
            nicFile.writelines(l)

    def removeVlan(self, vlan):
        self._backup(NET_CONF_PREF + vlan)
        self._removeFile(NET_CONF_PREF + vlan)

    def removeBonding(self, bonding):
        self._backup(NET_CONF_PREF + bonding)
        self._removeFile(NET_CONF_PREF + bonding)
        with open(BONDING_MASTERS, 'w') as f:
            f.write("-%s\n" % bonding)

    def removeBridge(self, bridge):
        self._backup(NET_CONF_PREF + bridge)
        self._removeFile(NET_CONF_PREF + bridge)

    def _updateConfigValue(self, conffile, entry, value):
        """
        Set value for network configuration file. If value is None, remove the
        entry from conffile.

        :param entry: entry to update (entry=value)
        :type entry: string

        :param value: value to update (entry=value)
        :type value: string

        Update conffile entry with the given value.
        """
        with open(conffile) as f:
            entries = [line for line in f.readlines()
                       if not line.startswith(entry + '=')]

        if value is not None:
            entries.append('\n' + entry + '=' + value)
        self._backup(conffile)
        with open(conffile, 'w') as f:
            f.writelines(entries)

    def setIfaceMtu(self, iface, newmtu):
        cf = NET_CONF_PREF + iface
        self._updateConfigValue(cf, 'MTU', str(newmtu))

    def setBondingMtu(self, bonding, newmtu):
        self.setIfaceMtu(bonding, newmtu)
        slaves = Bond(bonding).slaves
        for slave in slaves:
            self.setIfaceMtu(slave, newmtu)

    def dropBridgeParameter(self, iface_name):
        iface_conf_path = NET_CONF_PREF + iface_name

        if not os.path.isfile(iface_conf_path):
            return

        with open(iface_conf_path) as f:
            config_lines = f.readlines()

        config_lines_without_comments_and_bridge = [
            line for line in config_lines
            if (not line.startswith('#') and
                not line.startswith('BRIDGE'))]

        self.writeConfFile(iface_conf_path,
                           ''.join(config_lines_without_comments_and_bridge))


def stop_devices(device_ifcfgs):
    for dev in reversed(_sort_device_ifcfgs(device_ifcfgs)):
        ifdown(dev)
        if os.path.exists('/sys/class/net/%s/bridge' % dev):
            # ifdown is not enough to remove nicless bridges
            cmd.exec_sync([constants.EXT_BRCTL, 'delbr', dev])
        if _is_bond_name(dev):
            if _is_running_bond(dev):
                with open(BONDING_MASTERS, 'w') as f:
                    f.write("-%s\n" % dev)


def start_devices(device_ifcfgs):
    for dev in _sort_device_ifcfgs(device_ifcfgs):
        try:
            # this is an ugly way to check if this is a bond but picking into
            # the ifcfg files is even worse.
            if _is_bond_name(dev):
                if not _is_running_bond(dev):
                    with open(BONDING_MASTERS, 'w') as masters:
                        masters.write('+%s\n' % dev)
            _exec_ifup_by_name(dev)
        except ConfigNetworkError:
            logging.error('Failed to ifup device %s during rollback.', dev,
                          exc_info=True)


def _is_bond_name(dev):
    return dev.startswith('bond') and '.' not in dev


def _is_running_bond(bond):
    with open(BONDING_MASTERS) as info:
        names = info.read().split()
    return bond in names


def _sort_device_ifcfgs(device_ifcfgs):
    devices = {'Bridge': [],
               'Vlan': [],
               'Slave': [],
               'Other': []}
    for conf_file in device_ifcfgs:
        if not conf_file.startswith(NET_CONF_PREF):
            continue
        try:
            with open(conf_file) as f:
                content = f.read()
        except IOError as e:
            if e.errno == os.errno.ENOENT:
                continue
            else:
                raise
        dev = conf_file[len(NET_CONF_PREF):]

        devices[_dev_type(content)].append(dev)

    return devices['Other'] + devices['Vlan'] + devices['Bridge']


def _dev_type(content):
    if re.search('^TYPE=Bridge$', content, re.MULTILINE):
        return "Bridge"
    elif re.search('^VLAN=yes$', content, re.MULTILINE):
        return "Vlan"
    elif re.search('^SLAVE=yes$', content, re.MULTILINE):
        return "Slave"
    else:
        return "Other"


def ifup(iface):
    """Bring up an interface"""
    _exec_ifup_by_name(iface, cgroup=None)


def ifdown(iface):
    "Bring down an interface"
    rc, _, _ = cmd.exec_sync([constants.EXT_IFDOWN, iface])
    return rc


def _exec_ifup_by_name(iface_name, cgroup=dhclient.DHCLIENT_CGROUP):
    """
    Actually bring up an interface (by name).
    """
    cmds = [constants.EXT_IFUP, iface_name]

    if cgroup:
        rc, out, err = cmd.exec_systemd_new_unit(cmds, slice_name=cgroup)
    else:
        rc, out, err = cmd.exec_sync(cmds)

    if rc != 0:
        # In /etc/sysconfig/network-scripts/ifup* the last line usually
        # contains the error reason.
        raise ConfigNetworkError(ERR_FAILED_IFUP, out[-1] if out else '')


def _exec_ifup(iface, cgroup=dhclient.DHCLIENT_CGROUP):
    """
    Bring up an interface.

    If IPv6 is requested, enable it before the actual ifup because it may have
    been disabled (during network restoration after boot, or by a previous
    setupNetworks). If IPv6 is not requested, disable it after ifup.
    """
    if iface.ipv6:
        _enable_ipv6(iface.name)

    _exec_ifup_by_name(iface.name, cgroup)

    if not iface.ipv6:
        _enable_ipv6(iface.name, enable=False)


def _enable_ipv6(device_name, enable=True):
    # In broken networks, the device (and thus its sysctl node) may be missing.
    with _ignore_missing_device(device_name, enable):
        sysctl.disable_ipv6(device_name, disable=not enable)


@contextmanager
def _ignore_missing_device(device_name, enable_ipv6):
    try:
        yield
    except IOError as e:
        if e.errno == errno.ENOENT:
            logging.warning("%s didn't exist (yet) and so IPv6 wasn't %sabled."
                            % (device_name, 'en' if enable_ipv6 else 'dis'))
        else:
            raise


def _ifup(iface, cgroup=dhclient.DHCLIENT_CGROUP):
    if not iface.blockingdhcp and (iface.ipv4.bootproto == 'dhcp' or
                                   iface.ipv6.dhcpv6):
        # wait for dhcp in another thread, so vdsm won't get stuck (BZ#498940)
        t = concurrent.thread(_exec_ifup,
                              name='ifup/%s' % iface,
                              args=(iface, cgroup))
        t.start()
    else:
        if not iface.master and (iface.ipv4 or iface.ipv6):
            if iface.ipv4:
                wait_for_ip = waitfor.waitfor_ipv4_addr
            elif iface.ipv6:
                wait_for_ip = waitfor.waitfor_ipv6_addr

            with wait_for_ip(iface.name):
                _exec_ifup(iface, cgroup)
        else:
            with waitfor.waitfor_linkup(iface.name):
                _exec_ifup(iface, cgroup)


def _restore_default_bond_options(bond_name, desired_options):
    """
    Restore the bond's options to defaults corresponding to the intended
    bonding mode.
    The logic of handling the defaults is embedded in the link.bond set options
    therefore, we use set options as a whole.

    This works around an initscripts design limitation: ifup only touches
    declared options and leaves other (possibly non-default) options as-is.
    """
    Bond(bond_name).set_options(parse_bond_options(desired_options))


def _get_mode_from_desired_options(desired_options):
    if 'mode' not in desired_options:
        return None

    desired_mode = desired_options['mode']
    for k, v in six.viewitems(BONDING_MODES_NAME_TO_NUMBER):
        if desired_mode in (k, v):
            return [k, v]
    raise Exception('Error translating bond mode.')


def configuredPorts(nets, bridge):
    """Returns the list of ports a bridge has"""
    ports = []
    for filePath in glob.iglob(NET_CONF_PREF + '*'):
        with open(filePath) as confFile:
            for line in confFile:
                if line.startswith('BRIDGE=' + bridge):
                    port = filePath[filePath.rindex('-') + 1:]
                    logging.debug('port %s found in ifcfg for %s', port,
                                  bridge)
                    ports.append(port)
                    break
    return ports


def _from_ifcfg_bool(value):
    return value == 'yes'


def _to_ifcfg_bool(value):
    return 'yes' if value else 'no'


def _get_unified_persistence_ifcfg():
    """generate the set of ifcfg files that result of the current unified
    persistent networks"""
    persistent_config = PersistentConfig()
    if not persistent_config:
        return set()

    IFCFG_PATH = NET_CONF_PREF + '%s'
    RULE_PATH = os.path.join(NET_CONF_DIR, 'rule-%s')
    ROUTE_PATH = os.path.join(NET_CONF_DIR, 'route-%s')
    ifcfgs = set()

    for bonding, bonding_attr in six.viewitems(persistent_config.bonds):
        bond_nics = set(bonding_attr.get('nics', []))
        ifcfgs.add(IFCFG_PATH % bonding)
        for nic in bond_nics:
            ifcfgs.add(IFCFG_PATH % nic)

    for network, network_attr in six.viewitems(persistent_config.networks):
        top_level_device = None

        nic = network_attr.get('nic')
        if nic:
            ifcfgs.add(IFCFG_PATH % nic)
            top_level_device = nic

        network_bonding = network_attr.get('bonding', None)
        if network_bonding:
            top_level_device = network_bonding

        vlan_id = network_attr.get('vlan')
        if vlan_id is not None:
            underlying_device = network_bonding or network_attr.get('nic', '')
            vlan_device = '.'.join([underlying_device, str(vlan_id)])
            top_level_device = vlan_device
            ifcfgs.add(IFCFG_PATH % vlan_device)

        if tobool(network_attr.get('bridged', True)):
            ifcfgs.add(IFCFG_PATH % network)
            top_level_device = network

        ifcfgs.add(RULE_PATH % top_level_device)
        ifcfgs.add(ROUTE_PATH % top_level_device)

    return ifcfgs
