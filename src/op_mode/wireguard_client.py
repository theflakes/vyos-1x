#!/usr/bin/env python3
#
# Copyright (C) 2021 VyOS maintainers and contributors
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 2 or later as
# published by the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import argparse
import os

from jinja2 import Template
from ipaddress import ip_interface

from vyos.template import is_ipv4
from vyos.template import is_ipv6
from vyos.util import cmd
from vyos.util import popen

if os.geteuid() != 0:
    exit("You need to have root privileges to run this script.\nPlease try again, this time using 'sudo'. Exiting.")

tmpl = """
[Interface]
PrivateKey = {{ privkey }}
{% if address is defined and address|length > 0 %}
Address = {{ address | join(', ')}}
{% endif %}

[Peer]
PublicKey = {{ system_pubkey }}
Endpoint = {{ server }}:{{ port }}
AllowedIPs = 0.0.0.0/0, ::/0
"""

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument("-i", "--interface", type=str, help='WireGuard interface the client is connecting to', required=True)
    parser.add_argument("-s", "--server", type=str, help='WireGuard server IPv4/IPv6 address or FQDN', required=True)
    parser.add_argument("-a", "--address", type=str, help='WireGuard client IPv4/IPv6 address', action='append')
    args = parser.parse_args()

    interface = args.interface
    wg_pubkey = cmd(f'wg show {interface} | grep "public key"').split(':')[-1].lstrip()
    wg_port = cmd(f'wg show {interface} | grep "listening port"').split(':')[-1].lstrip()

    # Generate WireGuard private key
    privkey,_ = popen('wg genkey')
    # Generate public key portion from given private key
    pubkey,_ = popen('wg pubkey', input=privkey)

    config = {
        'system_pubkey' : wg_pubkey,
        'privkey': privkey,
        'pubkey' : pubkey,
        'server' : args.server,
        'port' : wg_port,
        'address' : [],
    }

    if args.address:
        v4_addr = 0
        v6_addr = 0
        for tmp in args.address:
            try:
                config['address'].append(str(ip_interface(tmp)))
                if is_ipv4(tmp):
                    v4_addr += 1
                elif is_ipv6(tmp):
                    v6_addr += 1
            except:
                print(tmp)
                exit('Client IP address invalid!')

        if (v4_addr > 1) or (v6_addr > 1):
            exit('Client can only have one IPv4 and one IPv6 address.')

    tmp = Template(tmpl, trim_blocks=True).render(config)
    qrcode,err = popen('qrencode -t ansiutf8', input=tmp)

    print(f'\nWireGuard client configuration for interface: {interface}')
    print(tmp)
    print('\n')
    print(qrcode)
