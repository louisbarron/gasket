"""This is the controller side authentication app.
It communicates with an authentication server (hostapd, captive portal) via HTTP.
And with rule_manager which communicates with Faucet via changing the Faucet configuration file,
and sending it a SIGHUP.
"""
# pylint: disable=import-error

import argparse
import logging
import queue
import re
import signal
import sys

from gasket.auth_config import AuthConfig
from gasket import rule_manager
from gasket import auth_app_utils
from gasket.hostapd_conf import HostapdConf
from gasket import hostapd_socket_thread
from gasket.work_item import AuthWorkItem, DeauthWorkItem


class Proto(object):
    """Class for protocol constants.
    """
    ETHER_ARP = 0x0806
    ETHER_IPv4 = 0x0800
    ETHER_IPv6 = 0x86DD
    ETHER_EAPOL = 0x888E
    IP_TCP = 6
    IP_UDP = 17
    DHCP_CLIENT_PORT = 68
    DHCP_SERVER_PORT = 67
    DNS_PORT = 53
    HTTP_PORT = 80


LEARNED_MACS_REGEX = r"""learned_macs{dp_id="(0x[a-f0-9]+)",dp_name="([\w-]+)",n="(\d+)",port="(\d+)",vlan="(\d+)"}"""


class AuthApp(object):
    '''
    This class recieves messages hostapd_ctrl from the portal via
    UNIX DOMAIN sockets, about the a change of state of the users.
    This could be either a log on or a log off of a user.
    The information is then passed on to rule_manager which
    installs/removes any appropriate rules.
    '''


    config = None
    rule_man = None
    logger = None
    logname = 'auth_app'

    work_queue = None
    threads = []

    def __init__(self, config, logger):
        super(AuthApp, self).__init__()
        self.config = config
        self.logger = logger
        self.rule_man = rule_manager.RuleManager(self.config, self.logger)
        self.learned_macs_compiled_regex = re.compile(LEARNED_MACS_REGEX)
        self.work_queue = queue.Queue()

    def start(self):
        """Starts separate thread for each hostapd socket.
        And runs as the worker thread processing the (de)authentications/.

        Main Worker thread.
        """
        signal.signal(signal.SIGINT, self._handle_sigint)

        self.logger.info('Starting hostapd socket threads')
        print('Starting hostapd socket threads ...')

        for hostapd_name, conf in self.config.hostapds.items():
            hostapd_conf = HostapdConf(hostapd_name, conf)
            hst = hostapd_socket_thread.HostapdSocketThread(hostapd_conf, self.work_queue,
                                                            self.config.logger_location)
            self.logger.info('Starting thread %s', hst)
            hst.start()
            self.threads.append(hst)
            self.logger.info('Thread running')

        print('Started socket Threads.')
        self.logger.info('Starting worker thread.')
        while True:
            work_item = self.work_queue.get()

            self.logger.info('Got work from queue')
            if isinstance(work_item, AuthWorkItem):
                self.authenticate(work_item.mac, work_item.username, work_item.acllist)
            elif isinstance(work_item, DeauthWorkItem):
                self.deauthenticate(work_item.mac)
            else:
                self.logger.warn("Unsupported WorkItem type: %s", type(work_item))

    def _get_dp_name_and_port(self, mac):
        """Queries the prometheus faucet client,
         and returns the 'access port' that the mac address is connected on.
        Args:
             mac MAC address to find port for.
        Returns:
             dp name & port number.
        """
        # query faucets promethues.
        self.logger.info('querying prometheus')
        try:
            prom_mac_table, prom_name_dpid = auth_app_utils.scrape_prometheus_vars(self.config.prom_url,
                                                                                   ['learned_macs', 'faucet_config_dp_name'])
        except Exception as e:
            self.logger.exception(e)
            return '', -1
        self.logger.info('queried prometheus. mac_table:\n%s\n\nname_dpid:\n%s',
                         prom_mac_table, prom_name_dpid)
        ret_port = -1
        ret_dp_name = ""
        dp_port_mode = self.config.dp_port_mode
        for line in prom_mac_table:
            labels, float_as_mac = line.split(' ')
            macstr = auth_app_utils.float_to_mac(float_as_mac)
            self.logger.debug('float %s is mac %s', float_as_mac, macstr)
            if mac == macstr:
                # if this is also an access port, we have found the dpid and the port
                values = self.learned_macs_compiled_regex.match(labels)
                dpid, dp_name, n, port, vlan = values.groups()
                if dp_name in dp_port_mode and \
                        'interfaces' in dp_port_mode[dp_name] and \
                        int(port) in dp_port_mode[dp_name]['interfaces'] and \
                        'auth_mode' in dp_port_mode[dp_name]['interfaces'][int(port)] and \
                        dp_port_mode[dp_name]['interfaces'][int(port)]['auth_mode'] == 'access':
                    ret_port = int(port)
                    ret_dp_name = dp_name
                    break
        self.logger.info("name: %s port: %d", ret_dp_name, ret_port)
        return ret_dp_name, ret_port

    def authenticate(self, mac, user, acl_list):
        """Authenticates the user as specifed by adding ACL rules
        to the Faucet configuration file. Once added Faucet is signaled via SIGHUP.
        Args:
            mac (str): MAC Address.
            user (str): Username.
            acl_list (list of str): names of acls (in order of highest priority to lowest) to be applied.
        """
        self.logger.info("****authenticated: %s %s", mac, user)

        switchname, switchport = self._get_dp_name_and_port(mac)

        if switchname == '' or switchport == -1:
            self.logger.warn(
                "Error switchname '%s' or switchport '%d' is unknown. Cannot generate acls for authed user '%s' on MAC '%s'",
                switchname, switchport, user, mac)
            # TODO one or the other?
#            self.hapd_req.deauthenticate(mac)
#            self.hapd_req.disassociate(mac)
            return

        self.logger.info('found mac')

        success = self.rule_man.authenticate(user, mac, switchname, switchport, acl_list)

        # TODO probably shouldn't return success if the switch/port cannot be found.
        # but at this stage auth server (hostapd) can't do anything about it.
        # Perhaps look into the CoA radius thing, so that process looks like:
        #   - client 1x success, send to here.
        #   - can't find switch. return failure.
        #   - hostapd revokes auth, so now client is aware there was an error.
#        if not success:
            # TODO one or the other?
#            self.hapd_req.deauthenticate(mac)
#            self.hapd_req.disassociate(mac)

    def deauthenticate(self, mac, username=None):
        """Deauthenticates the mac and username by removing related acl rules
        from Faucet's config file.
        Args:
            mac (str): mac address string to deauth
            username (str): username to deauth.
        """
        self.logger.info('---deauthenticated: %s %s', mac, username)

        self.rule_man.deauthenticate(username, mac)
        # TODO possibly handle success somehow. However the client wpa_supplicant, etc,
        # will likley think it has logged off, so is there anything we can do from hostapd to
        # say they have not actually logged off.
        # EAP LOGOFF is a one way message (not ack-ed)

    def is_port_managed(self, dpid, port_num):
        """
        Args:
            dpid (int): datapath id.
            port_num (int): port number.
        Returns:
            datapath name (str) if this dpid & port combo are managed (provide authentication).
             otherwise None
        """
        # query prometheus for the dpid -> name.
        # use the name to look in auth.yaml for the datapath.
        # if the dp is there, then use the port.
        #    if the port is there and it is set to 'access' return true
        # otherwise return false.
        dp_names = auth_app_utils.scrape_prometheus_vars(self.config.prom_url, ['dp_status'])[0]
        dp_name = ''
        for l in dp_names:
            pattern = r'dp_id="0x{:x}",dp_name="([\w-]+)"}}'.format(dpid)
            match = re.search(pattern, l)
            if match:
                dp_name = match.group(1)
                break

        if dp_name in self.config.dp_port_mode:
            if port_num in self.config.dp_port_mode[dp_name]['interfaces']:
                if 'auth_mode' in self.config.dp_port_mode[dp_name]['interfaces'][port_num]:
                    mode = self.config.dp_port_mode[dp_name]['interfaces'][port_num]['auth_mode']
                    if mode == 'access':
                        return dp_name
        return None

    def port_status_handler(self, ryu_event):
        """Deauthenticates all hosts on a port if the port has gone down.
        """
        msg = ryu_event.msg
        ryu_dp = msg.datapath
        dpid = ryu_dp.id
        port = msg.desc.port_no

        port_status = msg.desc.state & msg.datapath.ofproto.OFPPS_LINK_DOWN
        self.logger.info('DPID %d, Port %d has changed status: %d', dpid, port, port_status)
        if port_status == 1: # port is down
            dp_name = self.is_port_managed(dpid, port)
            self.logger.debug('dp_name: %s', dp_name)
            if dp_name:
                removed_macs = self.rule_man.reset_port_acl(dp_name, port)
                self.logger.info('removed macs: %s', removed_macs)
                for mac in removed_macs:
                    self.logger.info('sending deauth for %s', mac)
#                    self.hapd_req.deauthenticate(mac)

                self.logger.debug('reset port completed')

    def _handle_sigint(self, sigid, frame):
        """Handles the SIGINT signal.
        Closes the hostapd control interfaces, and kills the main thread ('self.run').
        """
        self.logger.info('SIGINT Received - Killing hostapd socket threads ...')
        for t in self.threads:
            t.kill()
        self.logger.info('Threads killed')
        sys.exit()


if __name__ == "__main__":
    print('Parsing args ...')
    parser = argparse.ArgumentParser()
    parser.add_argument('config', metavar='config', type=str,
                        nargs=1, help='path to configuration file')
    args = parser.parse_args()
    config_filename = '/etc/ryu/faucet/gasket/auth.yaml'
    if args.config:
        config_filename = args.config[0]
    print('Loading config %s' % config_filename)
    auth_config = AuthConfig(config_filename)
    log = auth_app_utils.get_logger('auth_app', auth_config.logger_location, logging.DEBUG, 1)

    aa = AuthApp(auth_config, log)
    print('Running AuthApp')
    try:
        aa.start()
    except Exception as e:
        log.exception(e)
