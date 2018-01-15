"""hostpad socket interface classes.
"""
# pytype: disable=none-attr
# pytype: disable=attribute-error
# pytype: disable=name-error
# pytype: disable=wrong-keyword-args

import socket
import re
import uuid
from datetime import datetime
import os
import time

class HostapdCtrl(object):
    """Abstract class for the control interface to hostapd.
    May be either a UNIX socket, or UDP (IPv4 or IPv6).
    """
    soc = None
    logger = None
    cookie = None
    local_unix_sock_path = None
    attached = False
    socket_attempts = 500

    def request(self, cmd):
        """
        Args:
            cmd (str): command string to send.
        Returns:
            returns result of cmd.
        """
        if self.cookie:
            cmd = 'COOKIE=%s %s' % (self.cookie, cmd)
        self.logger.debug('request is "%s"', cmd)
        self.soc.send(cmd.encode())

        return self.receive(size=4096)

    def attach(self):
        """Sends the 'attach' command to hostapd.
        Has the effect of subscribing to hostapd's unsolicited
        events.
        Returns:
            True if successful. False otherwise.
        """
        d = self.request('ATTACH')
        self.attached = self._returned_ok(d)
        return self.attached

    def detach(self):
        """Detatches the socket from hostapd -
        unsubscribes from hostapd's unsolicited events.
        Returns:
            True if detach successful or not attached. false otherwise.
        """
        if self.attached:
            d = self.request('DETACH')
            self.attached = not self._returned_ok(d)
            return self.attached
        return True

    def mib(self):
        """Get MIB variables (dot1x, dot11, radius)
        Returns:
            str
        """
        return self.request('MIB')

    def get_config(self):
        """Show current running config
        Returns:
            running config converted to dict.
        """
        d = self.request('GET_CONFIG')
        return self._to_dict(d)

    def get_sta(self, mac):
        """Get MIB variables for one station
        Args:
            mac (str): addr of station to request MIB
        Returns:
            dict of station MIB
        """
        return self._to_dict(self.request('STA %s' % mac))

    def all_sta(self):
        """Get MIB variables for all stations.
        Returns:
            list of station dict MIBs
        """
        stas = {}
        d = self.request('STA-FIRST')
        mac_addr = d.split()[0]
        data = d.split()[1:]
        stas[mac_addr] = self._to_dict(data)

        while True:
            d = self.request('STA-NEXT %s' % mac_addr)
            if d == b'FAIL\n':
                break
            mac_addr = d.split()[0]
            data = d.split()[1:]
            stas[mac_addr] = self._to_dict(data)
        return stas

    def deauthenticate(self, mac):
        """Deauthenticate a station. Not Currently implemented.
        Args:
            mac (str): MAC address of station.
        Returns:
            True if successful, False otherwise.
        """
        return True
#        d = self.request('DEAUTHENTICATE %s' % mac)
#        return self._returned_ok(d)

    def disassociate(self, mac):
        """Disassociate a station. Not Currently implemented.
        Args:
            mac (str): MAC address of station.
        Returns:
            True if successful, False otherwise.
        """
#        d = self.request('DISASSOCIATE %s' % mac)
#       return self._returned_ok(d)
        return True

    def ping(self):
        """pings hostapd
        Returns:
            True if 'PONG' is received, false otherwise.
        """
        d = self.request('PING')
        self.logger.debug('PING - %s', d)
        return 'PONG' in str(d)

    def _to_dict(self, d):
        self.logger.debug(d)
        dic = {}
        for s in d.split('\\n'):
            try:
                k, v = s.split('=')
                dic[k] = v
            except ValueError:
                self.logger.info('line: %s cannot be split by "="', s)
        return dic

    def receive(self, size=4096):
        """Receives size bytes from socket. (Blocking)
        Args:
            size (int): number of bytes to recieve.
        Returns:
            str of data.
        """
        return str(self.soc.recv(size))

    def close(self):
        """Close the underlying control socket.
        """
        if self.detach():
            self.logger.debug('detatched from socket')
        else:
            self.logger.warning('could not detatch from socket.')
        self.soc.close()
        self.logger.debug('socket closed')
        if self.local_unix_sock_path:
            os.remove(self.local_unix_sock_path)
            self.logger.debug('unix socket path removed')

    def set_timeout(self, secs):
        """Set the timeout of the socket
        """
        self.soc.settimeout(secs)

    @staticmethod
    def _returned_ok(d):
        return d == b'OK\n'


class HostapdCtrlUNIX(HostapdCtrl):
    """UNIX socket interface class.
    """

    def __init__(self, path, logger):
        self.logger = logger
        self.soc = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        if len(path) > 107:
            logger.critical('hostapd ctrl socket path must be <= 108 bytes (including null terminator), was; %d bytes, %s',
                            len(path), path)
            raise RuntimeError('hostapd ctrl socket path must be <= 108 bytes (including null terminator), was; %d bytes, %s' %
                               (len(path), path))
        try:
            self.connect(path)
        except FileNotFoundError as e:
            logger.error('Unable to connect to socket. FileNotFoundError %s' % path)
            raise

        logger.info('Connected to UNIX Socket: %s', path)
        tmpfile = '/tmp/auth-sock-%s' % datetime.now().microsecond #str(uuid.uuid4())
        self.local_unix_sock_path = tmpfile
        try:
            self.soc.bind(tmpfile)
            logger.info('Bound to UNIX Socket: %s', path)
        except OSError as e:
            logger.error('Unable to bind to UNIX Socket: %s' % tmpfile)
            raise e

    def connect(self, addr):
        for i in range(self.socket_attempts):
            try:
                self.soc.connect(addr)
                return
            except FileNotFoundError:
                time.sleep(1 * i)
                continue
        raise FileNotFoundError

class HostapdCtrlUDP(HostapdCtrl):
    """UDP socket interface class
    """

    def __init__(self, family, sockaddr, bind_address, bind_port, hsoc_type, logger):
        self.logger = logger
        self.soc = socket.socket(family, socket.SOCK_DGRAM)
        if hsoc_type in ['portforward', 'ping-and-portforward']:
        logger.info('connecting')
        try:
            if bind_address is not None and bind_port is not None:
                self.bind((bind_address, bind_port))
            self.connect(sockaddr)
        # pytype: disable=name-error
        except ConnectionRefusedError as e:
            logger.error('Unable to connect to UDP socket. sockaddr %s' % sockaddr)
            raise
        logger.info('Connected to UDP Socket: %s', sockaddr)

    def connect(self, addr):
        for i in range(self.socket_attempts):
            try:
                self.soc.connect(addr)
                c = self.get_cookie()
                self.cookie = re.findall("=[0-9a-f]*'", c)[0][1:-1]
                self.logger.info('UDP Socket Cookie is %s', self.cookie)
                return
            except ConnectionRefusedError:
                self.logger.debug("Couldn't connect to UDP socket %s", addr)
                time.sleep(pow(1.01,  i))
                continue
        raise ConnectionRefusedError

    def get_cookie(self):
        """Get cookie for this control interface.
        Returns:
            str cookie.
        """
        return str(self.request('GET_COOKIE'))


def request_socket_udp(host, port, bind_address, bind_port, hsoc_type, logger):
    """Create a HostapdCtrlUDP class.
    Args:
        host (str): ipv4/ipv6/hostname of remote.
        port (int): port number
        logger (logger): logger object
    Returns:
        HostapdCtrlUDP object
    """
    # use the first addr found, if more than one
    # (such as the case with hostnames that resolve to both ipv6 and ipv4).
    addrinfo = socket.getaddrinfo(host, port, socktype=socket.SOCK_DGRAM)[0]
    return HostapdCtrlUDP(addrinfo[0], addrinfo[4], bind_address, bind_port, logger)


def unsolicited_socket_udp(host, port, bind_port, hsoc_type, logger):
    """Create a HostapdCtrlUDP class, and attaches for receiveing
    unsolicited events.
    Args:
        host (str): ipv4/ipv6/hostname of remote.
        port (int): port number
        logger (logger): logger object
    Returns:
        HostapdCtrlUDP object
    """
    s = request_socket_udp(host, port, bind_address, bind_port, hsoc_type, logger)
    s.attach()
    return s


def request_socket_unix(path, logger):
    """Create a HostapdCtrlUNIX class.
    Args:
        path (str): pathname to UNIX socket.
        logger (logger): logger object
    Returns:
        HostapdCtrlUNIX object
    """
    return HostapdCtrlUNIX(path, logger)


def unsolicited_socket_unix(path, logger):
    """Create a HostapdCtrlUNIX class, and attaches for receiving
    unsolicited events.
    Args:
        path (str): pathname to UNIX socket.
        logger (logger): logger object
    Returns:
        HostapdCtrlUNIX object
    """
    s = request_socket_unix(path, logger)
    s.attach()
    s.set_timeout(4)
    return s
