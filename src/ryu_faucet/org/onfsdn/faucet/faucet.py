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
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import logging
import os
import signal

import ipaddr

from config_parser import config_file_hash, dp_parser
from valve import valve_factory
from util import kill_on_exception, get_sys_prefix, get_logger, dpid_log

from ryu.base import app_manager
from ryu.controller.handler import CONFIG_DISPATCHER
from ryu.controller.handler import MAIN_DISPATCHER
from ryu.controller.handler import set_ev_cls
from ryu.controller import dpset
from ryu.controller import event
from ryu.controller import ofp_event
from ryu.lib import hub
from ryu.lib.packet import ethernet
from ryu.lib.packet import packet
from ryu.lib.packet import vlan as ryu_vlan
from ryu.ofproto import ofproto_v1_3, ether
from ryu.services.protocols.bgp.bgpspeaker import BGPSpeaker


class EventFaucetReconfigure(event.EventBase):
    pass


class EventFaucetResolveGateways(event.EventBase):
    pass


class EventFaucetHostExpire(event.EventBase):
    pass


class Faucet(app_manager.RyuApp):
    """A Ryu app that performs layer 2 switching with VLANs.

    The intelligence is largely provided by a Valve class. Faucet's role is
    mainly to perform set up and to provide a communication layer between ryu
    and valve.
    """
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    _CONTEXTS = {'dpset': dpset.DPSet}

    logname = 'faucet'
    exc_logname = logname + '.exception'

    def __init__(self, *args, **kwargs):
        super(Faucet, self).__init__(*args, **kwargs)

        # There doesnt seem to be a sensible method of getting command line
        # options into ryu apps. Instead I am using the environment variable
        # FAUCET_CONFIG to allow this to be set, if it is not set it will
        # default to valve.yaml
        sysprefix = get_sys_prefix()
        self.config_file = os.getenv(
            'FAUCET_CONFIG', sysprefix + '/etc/ryu/faucet/faucet.yaml')
        self.logfile = os.getenv(
            'FAUCET_LOG', sysprefix + '/var/log/ryu/faucet/faucet.log')
        self.exc_logfile = os.getenv(
            'FAUCET_EXCEPTION_LOG',
            sysprefix + '/var/log/ryu/faucet/faucet_exception.log')

        # Set the signal handler for reloading config file
        signal.signal(signal.SIGHUP, self.signal_handler)

        # Create dpset object for querying Ryu's DPSet application
        self.dpset = kwargs['dpset']

        # Setup logging
        self.logger = get_logger(
            self.logname, self.logfile, logging.DEBUG, 0)
        # Set up separate logging for exceptions
        self.exc_logger = get_logger(
            self.exc_logname, self.exc_logfile, logging.CRITICAL, 1)

        # Set up a valve object for each datapath
        self.valves = {}
        self.config_hashes, valve_dps = dp_parser(
            self.config_file, self.logname)
        for valve_dp in valve_dps:
            # pylint: disable=no-member
            valve = valve_factory(valve_dp)
            if valve is None:
                self.logger.error(
                    'Hardware type not supported for DP: %s', valve_dp.name)
            else:
                self.valves[valve_dp.dp_id] = valve(valve_dp, self.logname)

        self.gateway_resolve_request_thread = hub.spawn(
            self.gateway_resolve_request)
        self.host_expire_request_thread = hub.spawn(
            self.host_expire_request)

        self.dp_bgp_speakers = {}
        self.reset_bgp()

    def bgp_route_handler(self, path_change, vlan):
        prefix = ipaddr.IPNetwork(path_change.prefix)
        nexthop = ipaddr.IPAddress(path_change.nexthop)
        withdraw = path_change.is_withdraw
        flowmods = []
        valve = self.valves[vlan.dp_id]
        ryudp = self.dpset.get(valve.dp.dp_id)
        for connected_network in vlan.controller_ips:
            if nexthop in connected_network:
                if nexthop == connected_network.ip:
                    self.logger.error(
                        'BGP nexthop %s for prefix %s cannot be us',
                        nexthop, prefix)
                elif withdraw:
                    self.logger.info(
                        'BGP withdraw %s nexthop %s',
                        prefix, nexthop)
                    flowmods = valve.del_route(vlan, prefix)
                else:
                    self.logger.info(
                        'BGP add %s nexthop %s', prefix, nexthop)
                    flowmods = valve.add_route(vlan, nexthop, prefix)
                if flowmods:
                    self.send_flow_msgs(ryudp, flowmods)
                return
        self.logger.error(
            'BGP nexthop %s for prefix %s is not a connected network',
            nexthop, prefix)

    def reset_bgp(self):
        # TODO: port status changes should cause us to withdraw a route.
        # TODO: configurable behavior - withdraw routes if peer goes down.
        for dp_id, valve in self.valves.iteritems():
            if dp_id not in self.dp_bgp_speakers:
                self.dp_bgp_speakers[dp_id] = {}
            bgp_speakers = self.dp_bgp_speakers[dp_id]
            for bgp_speaker in bgp_speakers.itervalues():
                bgp_speaker.shutdown()
            for vlan in valve.dp.vlans.itervalues():
                if vlan.bgp_as:
                    handler = lambda x: self.bgp_route_handler(x, vlan)
                    bgp_speaker = BGPSpeaker(
                        as_number=vlan.bgp_as,
                        router_id=vlan.bgp_routerid,
                        bgp_server_port=vlan.bgp_port,
                        best_path_change_handler=handler)
                    for controller_ip in vlan.controller_ips:
                        prefix = ipaddr.IPNetwork(
                            '/'.join(
                                (str(controller_ip.ip),
                                 str(controller_ip.prefixlen))))
                        bgp_speaker.prefix_add(
                            prefix=str(prefix),
                            next_hop=controller_ip.ip)
                    for route_table in (vlan.ipv4_routes, vlan.ipv6_routes):
                        for ip_dst, ip_gw in route_table.iteritems():
                            bgp_speaker.prefix_add(
                                prefix=str(ip_dst),
                                next_hop=str(ip_gw))
                    bgp_speaker.neighbor_add(
                        address=vlan.bgp_neighbor_address,
                        remote_as=vlan.bgp_neighbor_as)
                    bgp_speakers[vlan] = bgp_speaker

    def gateway_resolve_request(self):
        while True:
            self.send_event('Faucet', EventFaucetResolveGateways())
            hub.sleep(2)

    def host_expire_request(self):
        while True:
            self.send_event('Faucet', EventFaucetHostExpire())
            hub.sleep(5)

    def send_flow_msgs(self, ryu_dp, flow_msgs):
        dp_id = ryu_dp.id
        if dp_id not in self.valves:
            self.logger.error('send_flow_msgs: unknown %s', dpid_log(dp_id))
            return
        self.valves[dp_id].ofchannel_log(flow_msgs)
        for flow_msg in flow_msgs:
            flow_msg.datapath = ryu_dp
            ryu_dp.send_msg(flow_msg)

    def signal_handler(self, sigid, frame):
        if sigid == signal.SIGHUP:
            self.send_event('Faucet', EventFaucetReconfigure())

    def config_changed(self, new_config_file):
        if new_config_file != self.config_file:
            return True
        for config_file, config_hash in self.config_hashes.iteritems():
            config_file_exists = os.path.isfile(config_file)
            # Config file not loaded but exists = reload.
            if config_hash is None and config_file_exists:
                return True
            # Config file loaded but no longer exists = reload.
            if config_hash and not config_file_exists:
                return True
            # Config file hash has changed = reload.
            new_config_hash = config_file_hash(config_file)
            if new_config_hash != config_hash:
                return True
        return False

    @set_ev_cls(EventFaucetReconfigure, MAIN_DISPATCHER)
    def reload_config(self, ryu_event):
        new_config_file = os.getenv('FAUCET_CONFIG', self.config_file)
        if not self.config_changed(new_config_file):
            self.logger.info('configuration is unchanged, not reloading')
            return
        self.config_file = new_config_file
        self.config_hashes, new_dps = dp_parser(new_config_file, self.logname)
        for new_dp in new_dps:
            # pylint: disable=no-member
            flowmods = self.valves[new_dp.dp_id].reload_config(new_dp)
            ryudp = self.dpset.get(new_dp.dp_id)
            self.send_flow_msgs(ryudp, flowmods)
            self.reset_bgp()

    @set_ev_cls(EventFaucetResolveGateways, MAIN_DISPATCHER)
    def resolve_gateways(self, ryu_event):
        for dp_id, valve in self.valves.iteritems():
            flowmods = valve.resolve_gateways()
            if flowmods:
                ryudp = self.dpset.get(dp_id)
                self.send_flow_msgs(ryudp, flowmods)

    @set_ev_cls(EventFaucetHostExpire, MAIN_DISPATCHER)
    def host_expire(self, ryu_event):
        for valve in self.valves.values():
            valve.host_expire()

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER) # pylint: disable=no-member
    @kill_on_exception(exc_logname)
    def _packet_in_handler(self, ryu_event):
        msg = ryu_event.msg
        ryu_dp = msg.datapath
        dp_id = ryu_dp.id

        if not dp_id in self.valves:
            self.logger.error('_packet_in_handler: unknown %s', dpid_log(dp_id))
            return

        valve = self.valves[dp_id]
        valve.ofchannel_log([msg])

        pkt = packet.Packet(msg.data)
        eth_pkt = pkt.get_protocols(ethernet.ethernet)[0]
        eth_type = eth_pkt.ethertype

        if eth_type == ether.ETH_TYPE_8021Q:
            # tagged packet
            vlan_proto = pkt.get_protocols(ryu_vlan.vlan)[0]
            vlan_vid = vlan_proto.vid
        else:
            return

        in_port = msg.match['in_port']
        flowmods = valve.rcv_packet(dp_id, in_port, vlan_vid, pkt)
        self.send_flow_msgs(ryu_dp, flowmods)

    @set_ev_cls(ofp_event.EventOFPErrorMsg, MAIN_DISPATCHER) # pylint: disable=no-member
    @kill_on_exception(exc_logname)
    def _error_handler(self, ryu_event):
        msg = ryu_event.msg
        ryu_dp = msg.datapath
        dp_id = ryu_dp.id
        if dp_id in self.valves:
            self.valves[dp_id].ofchannel_log([msg])
            self.logger.error('Got OFError: %s', msg)
        else:
            self.logger.error('_error_handler: unknown %s', dpid_log(dp_id))

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER) # # pylint: disable=no-member
    def handler_features(self, ryu_event):
        msg = ryu_event.msg
        ryu_dp = msg.datapath
        dp_id = ryu_dp.id
        if dp_id in self.valves:
            flowmods = self.valves[dp_id].switch_features(dp_id, msg)
            self.send_flow_msgs(ryu_dp, flowmods)
        else:
            self.logger.error('handler_features: unknown %s', dpid_log(dp_id))

    @set_ev_cls(dpset.EventDP, dpset.DPSET_EV_DISPATCHER)
    @kill_on_exception(exc_logname)
    def handler_connect_or_disconnect(self, ryu_event):
        ryu_dp = ryu_event.dp
        dp_id = ryu_dp.id

        if not ryu_event.enter:
            if dp_id in self.valves:
                # Datapath down message
                self.logger.debug('%s disconnected', dpid_log(dp_id))
                self.valves[dp_id].datapath_disconnect(dp_id)
            else:
                self.logger.error(
                    'handler_connect_or_disconnect: unknown %s', dpid_log(dp_id))
            return

        self.logger.debug('%s connected', dpid_log(dp_id))
        self.handler_datapath(ryu_dp)

    @set_ev_cls(dpset.EventDPReconnected, dpset.DPSET_EV_DISPATCHER)
    @kill_on_exception(exc_logname)
    def handler_reconnect(self, ryu_event):
        ryu_dp = ryu_event.dp
        self.logger.debug('%s reconnected', dpid_log(ryu_dp.id))
        self.handler_datapath(ryu_dp)

    def handler_datapath(self, ryu_dp):
        dp_id = ryu_dp.id
        if dp_id in self.valves:
            discovered_ports = [
                p.port_no for p in ryu_dp.ports.values() if p.state == 0]
            flowmods = self.valves[dp_id].datapath_connect(dp_id, discovered_ports)
            self.send_flow_msgs(ryu_dp, flowmods)
        else:
            self.logger.error('handler_datapath: unknown %s', dpid_log(dp_id))

    @set_ev_cls(ofp_event.EventOFPPortStatus, MAIN_DISPATCHER) # pylint: disable=no-member
    @kill_on_exception(exc_logname)
    def port_status_handler(self, ryu_event):
        msg = ryu_event.msg
        ryu_dp = msg.datapath
        dp_id = ryu_dp.id
        ofp = msg.datapath.ofproto
        reason = msg.reason
        port_no = msg.desc.port_no

        if dp_id not in self.valves:
            self.logger.error(
                'port_status_handler: unknown %s', dpid_log(dp_id))
            return

        valve = self.valves[dp_id]
        flowmods = []
        if reason == ofp.OFPPR_ADD:
            flowmods = valve.port_add(dp_id, port_no)
        elif reason == ofp.OFPPR_DELETE:
            flowmods = valve.port_delete(dp_id, port_no)
        elif reason == ofp.OFPPR_MODIFY:
            port_down = msg.desc.state & ofp.OFPPS_LINK_DOWN
            if port_down:
                flowmods = valve.port_delete(dp_id, port_no)
            else:
                flowmods = valve.port_add(dp_id, port_no)
        else:
            self.logger.warning('Unhandled port status %s for port %u',
                                reason, port_no)

        self.send_flow_msgs(ryu_dp, flowmods)
