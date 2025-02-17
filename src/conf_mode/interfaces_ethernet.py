#!/usr/bin/env python3
#
# Copyright (C) 2019-2024 VyOS maintainers and contributors
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 2 or later as
# published by the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import os

from sys import exit

from vyos.base import Warning
from vyos.config import Config
from vyos.configdict import get_interface_dict
from vyos.configdict import is_node_changed
from vyos.configverify import verify_address
from vyos.configverify import verify_dhcpv6
from vyos.configverify import verify_interface_exists
from vyos.configverify import verify_mirror_redirect
from vyos.configverify import verify_mtu
from vyos.configverify import verify_mtu_ipv6
from vyos.configverify import verify_vlan_config
from vyos.configverify import verify_vrf
from vyos.configverify import verify_bond_bridge_member
from vyos.configverify import verify_pki_certificate
from vyos.configverify import verify_pki_ca_certificate
from vyos.ethtool import Ethtool
from vyos.ifconfig import EthernetIf
from vyos.ifconfig import BondIf
from vyos.pki import find_chain
from vyos.pki import encode_certificate
from vyos.pki import load_certificate
from vyos.pki import wrap_private_key
from vyos.template import render
from vyos.utils.process import call
from vyos.utils.dict import dict_search
from vyos.utils.dict import dict_to_paths_values
from vyos.utils.dict import dict_set
from vyos.utils.dict import dict_delete
from vyos.utils.file import write_file
from vyos import ConfigError
from vyos import airbag
airbag.enable()

# XXX: wpa_supplicant works on the source interface
cfg_dir = '/run/wpa_supplicant'
wpa_suppl_conf = '/run/wpa_supplicant/{ifname}.conf'

def update_bond_options(conf: Config, eth_conf: dict) -> list:
    """
    Return list of blocked options if interface is a bond member
    :param conf: Config object
    :type conf: Config
    :param eth_conf: Ethernet config dictionary
    :type eth_conf: dict
    :return: List of blocked options
    :rtype: list
    """
    blocked_list = []
    bond_name = list(eth_conf['is_bond_member'].keys())[0]
    config_without_defaults = conf.get_config_dict(
        ['interfaces', 'ethernet', eth_conf['ifname']],
        key_mangling=('-', '_'),
        get_first_key=True,
        no_tag_node_value_mangle=True,
        with_defaults=False,
        with_recursive_defaults=False)
    config_with_defaults = conf.get_config_dict(
        ['interfaces', 'ethernet', eth_conf['ifname']],
        key_mangling=('-', '_'),
        get_first_key=True,
        no_tag_node_value_mangle=True,
        with_defaults=True,
        with_recursive_defaults=True)
    bond_config_with_defaults = conf.get_config_dict(
        ['interfaces', 'bonding', bond_name],
        key_mangling=('-', '_'),
        get_first_key=True,
        no_tag_node_value_mangle=True,
        with_defaults=True,
        with_recursive_defaults=True)
    eth_dict_paths = dict_to_paths_values(config_without_defaults)
    eth_path_base = ['interfaces', 'ethernet', eth_conf['ifname']]

    #if option is configured under ethernet section
    for option_path, option_value in eth_dict_paths.items():
        bond_option_value = dict_search(option_path, bond_config_with_defaults)

        #If option is allowed for changing then continue
        if option_path in EthernetIf.get_bond_member_allowed_options():
            continue
        # if option is inherited from bond then set valued from bond interface
        if option_path in BondIf.get_inherit_bond_options():
            # If option equals to bond option then do nothing
            if option_value == bond_option_value:
                continue
            else:
                # if ethernet has option and bond interface has
                # then copy it from bond
                if bond_option_value is not None:
                    if is_node_changed(conf, eth_path_base + option_path.split('.')):
                        Warning(
                            f'Cannot apply "{option_path.replace(".", " ")}" to "{option_value}".' \
                            f' Interface "{eth_conf["ifname"]}" is a bond member.' \
                            f' Option is inherited from bond "{bond_name}"')
                    dict_set(option_path, bond_option_value, eth_conf)
                    continue
                # if ethernet has option and bond interface does not have
                # then delete it form dict and do not apply it
                else:
                    if is_node_changed(conf, eth_path_base + option_path.split('.')):
                        Warning(
                            f'Cannot apply "{option_path.replace(".", " ")}".' \
                            f' Interface "{eth_conf["ifname"]}" is a bond member.' \
                            f' Option is inherited from bond "{bond_name}"')
                    dict_delete(option_path, eth_conf)
        blocked_list.append(option_path)

    # if inherited option is not configured under ethernet section but configured under bond section
    for option_path in BondIf.get_inherit_bond_options():
        bond_option_value = dict_search(option_path, bond_config_with_defaults)
        if bond_option_value is not None:
            if option_path not in eth_dict_paths:
                if is_node_changed(conf, eth_path_base + option_path.split('.')):
                    Warning(
                        f'Cannot apply "{option_path.replace(".", " ")}" to "{dict_search(option_path, config_with_defaults)}".' \
                        f' Interface "{eth_conf["ifname"]}" is a bond member. ' \
                        f'Option is inherited from bond "{bond_name}"')
                dict_set(option_path, bond_option_value, eth_conf)
    eth_conf['bond_blocked_changes'] = blocked_list
    return None

def get_config(config=None):
    """
    Retrive CLI config as dictionary. Dictionary can never be empty, as at least the
    interface name will be added or a deleted flag
    """
    if config:
        conf = config
    else:
        conf = Config()

    base = ['interfaces', 'ethernet']
    ifname, ethernet = get_interface_dict(conf, base, with_pki=True)

    if 'is_bond_member' in ethernet:
        update_bond_options(conf, ethernet)

    tmp = is_node_changed(conf, base + [ifname, 'speed'])
    if tmp: ethernet.update({'speed_duplex_changed': {}})

    tmp = is_node_changed(conf, base + [ifname, 'duplex'])
    if tmp: ethernet.update({'speed_duplex_changed': {}})

    return ethernet

def verify_speed_duplex(ethernet: dict, ethtool: Ethtool):
    """
     Verify speed and duplex
    :param ethernet: dictionary which is received from get_interface_dict
    :type ethernet: dict
    :param ethtool: Ethernet object
    :type ethtool: Ethtool
    """
    if ((ethernet['speed'] == 'auto' and ethernet['duplex'] != 'auto') or
            (ethernet['speed'] != 'auto' and ethernet['duplex'] == 'auto')):
        raise ConfigError(
            'Speed/Duplex missmatch. Must be both auto or manually configured')

    if ethernet['speed'] != 'auto' and ethernet['duplex'] != 'auto':
        # We need to verify if the requested speed and duplex setting is
        # supported by the underlaying NIC.
        speed = ethernet['speed']
        duplex = ethernet['duplex']
        if not ethtool.check_speed_duplex(speed, duplex):
            raise ConfigError(
                f'Adapter does not support changing speed ' \
                f'and duplex settings to: {speed}/{duplex}!')


def verify_flow_control(ethernet: dict, ethtool: Ethtool):
    """
     Verify flow control
    :param ethernet: dictionary which is received from get_interface_dict
    :type ethernet: dict
    :param ethtool: Ethernet object
    :type ethtool: Ethtool
    """
    if 'disable_flow_control' in ethernet:
        if not ethtool.check_flow_control():
            raise ConfigError(
                'Adapter does not support changing flow-control settings!')


def verify_ring_buffer(ethernet: dict, ethtool: Ethtool):
    """
     Verify ring buffer
    :param ethernet: dictionary which is received from get_interface_dict
    :type ethernet: dict
    :param ethtool: Ethernet object
    :type ethtool: Ethtool
    """
    if 'ring_buffer' in ethernet:
        max_rx = ethtool.get_ring_buffer_max('rx')
        if not max_rx:
            raise ConfigError(
                'Driver does not support RX ring-buffer configuration!')

        max_tx = ethtool.get_ring_buffer_max('tx')
        if not max_tx:
            raise ConfigError(
                'Driver does not support TX ring-buffer configuration!')

        rx = dict_search('ring_buffer.rx', ethernet)
        if rx and int(rx) > int(max_rx):
            raise ConfigError(f'Driver only supports a maximum RX ring-buffer ' \
                              f'size of "{max_rx}" bytes!')

        tx = dict_search('ring_buffer.tx', ethernet)
        if tx and int(tx) > int(max_tx):
            raise ConfigError(f'Driver only supports a maximum TX ring-buffer ' \
                              f'size of "{max_tx}" bytes!')


def verify_offload(ethernet: dict, ethtool: Ethtool):
    """
     Verify offloading capabilities
    :param ethernet: dictionary which is received from get_interface_dict
    :type ethernet: dict
    :param ethtool: Ethernet object
    :type ethtool: Ethtool
    """
    if dict_search('offload.rps', ethernet) != None:
        if not os.path.exists(f'/sys/class/net/{ethernet["ifname"]}/queues/rx-0/rps_cpus'):
            raise ConfigError('Interface does not suport RPS!')
    driver = ethtool.get_driver_name()
    # T3342 - Xen driver requires special treatment
    if driver == 'vif':
        if int(ethernet['mtu']) > 1500 and dict_search('offload.sg', ethernet) == None:
            raise ConfigError('Xen netback drivers requires scatter-gatter offloading '\
                              'for MTU size larger then 1500 bytes')


def verify_allowedbond_changes(ethernet: dict):
    """
     Verify changed options if interface is in bonding
    :param ethernet: dictionary which is received from get_interface_dict
    :type ethernet: dict
    """
    if 'bond_blocked_changes' in ethernet:
        for option in ethernet['bond_blocked_changes']:
            raise ConfigError(f'Cannot configure "{option.replace(".", " ")}"' \
                              f' on interface "{ethernet["ifname"]}".' \
                              f' Interface is a bond member')

def verify_eapol(ethernet: dict):
    """
    Common helper function used by interface implementations to perform
    recurring validation of EAPoL configuration.
    """
    if 'eapol' not in ethernet:
        return

    if 'certificate' not in ethernet['eapol']:
        raise ConfigError('Certificate must be specified when using EAPoL!')

    verify_pki_certificate(ethernet, ethernet['eapol']['certificate'], no_password_protected=True)

    if 'ca_certificate' in ethernet['eapol']:
        for ca_cert in ethernet['eapol']['ca_certificate']:
            verify_pki_ca_certificate(ethernet, ca_cert)

def verify(ethernet):
    if 'deleted' in ethernet:
        return None
    if 'is_bond_member' in ethernet:
        verify_bond_member(ethernet)
    else:
        verify_ethernet(ethernet)


def verify_bond_member(ethernet):
    """
     Verification function for ethernet interface which is in bonding
    :param ethernet: dictionary which is received from get_interface_dict
    :type ethernet: dict
    """
    ifname = ethernet['ifname']
    verify_interface_exists(ifname)
    verify_eapol(ethernet)
    verify_mirror_redirect(ethernet)
    ethtool = Ethtool(ifname)
    verify_speed_duplex(ethernet, ethtool)
    verify_flow_control(ethernet, ethtool)
    verify_ring_buffer(ethernet, ethtool)
    verify_offload(ethernet, ethtool)
    verify_allowedbond_changes(ethernet)

def verify_ethernet(ethernet):
    """
     Verification function for simple ethernet interface
    :param ethernet: dictionary which is received from get_interface_dict
    :type ethernet: dict
    """
    ifname = ethernet['ifname']
    verify_interface_exists(ifname)
    verify_mtu(ethernet)
    verify_mtu_ipv6(ethernet)
    verify_dhcpv6(ethernet)
    verify_address(ethernet)
    verify_vrf(ethernet)
    verify_bond_bridge_member(ethernet)
    verify_eapol(ethernet)
    verify_mirror_redirect(ethernet)
    ethtool = Ethtool(ifname)
    # No need to check speed and duplex keys as both have default values.
    verify_speed_duplex(ethernet, ethtool)
    verify_flow_control(ethernet, ethtool)
    verify_ring_buffer(ethernet, ethtool)
    verify_offload(ethernet, ethtool)
    # use common function to verify VLAN configuration
    verify_vlan_config(ethernet)
    return None


def generate(ethernet):
    # render real configuration file once
    wpa_supplicant_conf = wpa_suppl_conf.format(**ethernet)

    if 'deleted' in ethernet:
        # delete configuration on interface removal
        if os.path.isfile(wpa_supplicant_conf):
            os.unlink(wpa_supplicant_conf)
        return None

    if 'eapol' in ethernet:
        ifname = ethernet['ifname']

        render(wpa_supplicant_conf, 'ethernet/wpa_supplicant.conf.j2', ethernet)

        cert_file_path = os.path.join(cfg_dir, f'{ifname}_cert.pem')
        cert_key_path = os.path.join(cfg_dir, f'{ifname}_cert.key')

        cert_name = ethernet['eapol']['certificate']
        pki_cert = ethernet['pki']['certificate'][cert_name]

        loaded_pki_cert = load_certificate(pki_cert['certificate'])
        loaded_ca_certs = {load_certificate(c['certificate'])
            for c in ethernet['pki']['ca'].values()} if 'ca' in ethernet['pki'] else {}

        cert_full_chain = find_chain(loaded_pki_cert, loaded_ca_certs)

        write_file(cert_file_path,
                   '\n'.join(encode_certificate(c) for c in cert_full_chain))
        write_file(cert_key_path, wrap_private_key(pki_cert['private']['key']))

        if 'ca_certificate' in ethernet['eapol']:
            ca_cert_file_path = os.path.join(cfg_dir, f'{ifname}_ca.pem')
            ca_chains = []

            for ca_cert_name in ethernet['eapol']['ca_certificate']:
                pki_ca_cert = ethernet['pki']['ca'][ca_cert_name]
                loaded_ca_cert = load_certificate(pki_ca_cert['certificate'])
                ca_full_chain = find_chain(loaded_ca_cert, loaded_ca_certs)
                ca_chains.append(
                    '\n'.join(encode_certificate(c) for c in ca_full_chain))

            write_file(ca_cert_file_path, '\n'.join(ca_chains))

    return None

def apply(ethernet):
    ifname = ethernet['ifname']
    # take care about EAPoL supplicant daemon
    eapol_action='stop'

    e = EthernetIf(ifname)
    if 'deleted' in ethernet:
        # delete interface
        e.remove()
    else:
        e.update(ethernet)
        if 'eapol' in ethernet:
            eapol_action='reload-or-restart'

    call(f'systemctl {eapol_action} wpa_supplicant-wired@{ifname}')

if __name__ == '__main__':
    try:
        c = get_config()
        verify(c)
        generate(c)

        apply(c)
    except ConfigError as e:
        print(e)
        exit(1)
