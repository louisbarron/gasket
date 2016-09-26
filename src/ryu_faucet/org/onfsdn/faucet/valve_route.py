# Copyright (C) 2013 Nippon Telegraph and Telephone Corporation.
# Copyright (C) 2015 Brad Cowie, Christopher Lorier and Joe Stringer.
# Copyright (C) 2015 Research and Education Advanced Network New Zealand Ltd.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASISo
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import valve_of
import valve_packet

from ryu.ofproto import ether


class ValveRouteManager(object):

    def __init__(self, logger, faucet_mac, fib_table):
        self.logger = logger
        self.faucet_mac = faucet_mac
        self.fib_table = fib_table

    def vlan_vid(self, vlan, in_port):
        vid = None
        if vlan.port_is_tagged(in_port):
            vid = vlan.vid
        return vid

    def neighbor_resolver_pkt(self, vid, controller_ip, ip_gw):
        pass

    def neighbor_resolver(self, ip_gw, controller_ip, vlan, ports):
        ofmsgs = []
        if ports:
            self.logger.info('Resolving %s', ip_gw)
            port_num = ports[0].number
            vid = self.vlan_vid(vlan, port_num)
            resolver_pkt = self.neighbor_resolver_pkt(vid, controller_ip, ip_gw)
            for port in ports:
                ofmsgs.append(valve_of.packetout(port.number, resolver_pkt.data))
        return ofmsgs


class ValveIPv4RouteManager(ValveRouteManager):

    def eth_type(self):
        return ether.ETH_TYPE_IP

    def vlan_routes(self, vlan):
        return vlan.ipv4_routes

    def vlan_neighbor_cache(self, vlan):
        return vlan.arp_cache

    def neighbor_resolver_pkt(self, vid, controller_ip, ip_gw):
        return valve_packet.arp_request(
            self.faucet_mac, vid, controller_ip.ip, ip_gw)


class ValveIPv6RouteManager(ValveRouteManager):

    def eth_type(self):
        return ether.ETH_TYPE_IPV6

    def vlan_routes(self, vlan):
        return vlan.ipv6_routes

    def vlan_neighbor_cache(self, vlan):
        return vlan.nd_cache

    def neighbor_resolver_pkt(self, vid, controller_ip, ip_gw):
        return valve_packet.nd_request(
            self.faucet_mac, vid, controller_ip.ip, ip_gw)
