"""Handles the construction of the Faucet ACL configuration from the authentication application."""
# pytype: disable=pyi-error
import logging
import os
import re
import shutil
import signal
import sys
import time
# pytype: disable=pyi-error
import yaml

import requests

from gasket.rule_generator import RuleGenerator
from gasket import auth_app_utils

def main():
    """Create a default base config and the initial Faucet ACL yaml file,
    from a 'base' yaml file.
    """
    # pylint: disable=unbalanced-tuple-unpacking
    input_f, output_f = sys.argv[1:3]

    create_base_faucet_acls(input_f, output_f)

    # this is the original, (no one authenticated file). restore to this if necessary.
    shutil.copy2(input_f, input_f + '-orig')


def create_base_faucet_acls(input_f, output_f):
    """
    Args:
        input_f (str): input filename (base config)
        output_f (str): output filename (faucet-acl.yaml)
    """
    with open(input_f) as f:
        base = yaml.safe_load(f)
    logging.basicConfig(filename='rule_man_base.log', level=logging.DEBUG)
    final = create_faucet_acls(base, logger=logging)
    write_yaml(final, output_f, True)


def create_faucet_acls(doc, logger):
    """Creates a yaml object that represents faucet acls.
    Args:
        doc (yaml object): yaml dict. containing the pre-faucet version of the
                            acls.
    Returns: yaml object {'acls': ...}
    """
    final = {}
    final['acls'] = {}
    final_acls = final['acls']

    for acl_name, acl in list(doc['acls'].items()):
        seq = []
        for obj in acl:
            if isinstance(obj, dict) and 'rule' in obj:
                # rule
                for _, rule in list(obj.items()):
                    # TODO is this a pointless for loop? instead do rule = obj['rule']
                    # TODO can thes ifs by made a function as reused?
                    new_rule = {}
                    new_rule['rule'] = rule
                    if '_mac_' in rule:
                        del rule['_mac_']
                    if '_name_' in rule:
                        del rule['_name_']
                    seq.append(new_rule)
            elif isinstance(obj, dict):
                #alias
                for name, l in list(obj.items()):
                    for rule in l:
                        r = rule['rule']
                        if '_mac_' in r:
                            del r['_mac_']
                        if '_name_' in r:
                            del r['_name_']
                        seq.append(rule)
            elif isinstance(obj, list):
                for y in obj:
                    if isinstance(y, dict):
                        # list of dicts
                        for _, rule in list(y.items()):
                            new_rule = {}
                            new_rule['rule'] = rule
                            if '_mac_' in rule:
                                del rule['_mac_']
                            if '_name_' in rule:
                                del rule['_name_']
                            seq.append(new_rule)
                    else:
                        logger.warning('list of unrecognised objects')
                        logger.warning('child type: %s' % type(y))
                        logger.warning('list object: %s' % obj)
            elif isinstance(obj, str):
                # this is likey just a 'flag' used to mark position to insert the rules when authed
                if obj == 'authed-rules':
                    continue
                else:
                    logger.warning('illegal string %s', obj)
            else:
                logger.warning('Object type %s not recognised', type(obj))
                logger.warning('Object: %s', obj)

        final_acls[acl_name] = seq
    return final


def write_yaml(yml, filename, ignore_aliases=False):
    """Writes a yaml object to file.
    Args:
        yml (yaml): yaml object to write to file.
        filename (str)
        ignore_aliases (bool): True if yaml aliases should be removed
                                and object written out in full.
                                False if aliases can be used.
    """
    if ignore_aliases:
        noalias_dumper = yaml.dumper.SafeDumper
        noalias_dumper.ignore_aliases = lambda self, data: True
        with open(filename, 'w') as f:
            yaml.dump(yml, f, default_flow_style=False, Dumper=noalias_dumper)
    else:
        with open(filename, 'w') as f:
            yaml.dump(yml, f, default_flow_style=False)


class RuleManager(object):
    """Handles the construction of the Faucet ACL configuration from the authentication
    application.
    """

    logger = None

    def __init__(self, config, logger):
        self.config = config
        self.logger = logger
        self.rule_gen = RuleGenerator(self.config.rules, self.logger)
        self.base_filename = self.config.base_filename
        self.faucet_acl_filename = self.config.acl_config_file

    def add_to_base_acls(self, filename, rules, user, mac):
        '''Adds rules to the base acl file (and writes).
        Args:
            filename (str);
            rules (dict): {port_s1_1 : list of rules}
            user (str): username
        '''
        with open(filename) as f:
            base = yaml.safe_load(f)
        # somehow add the rules to the base where ideally the items in the acl are the pointers.
        # but guess it might not matter, just hurts readability.

        # this is NOT a spelling mistake. this ensures that the auth rules are defined before
        # the use in the port acl.
        # and that the port acl will have the pointer. At the end of the day it doesn't matter.
        if 'aauth' not in base:
            base['aauth'] = {}

        for aclname, acllist in list(rules.items()):
            self.logger.debug('base %s', base)
            self.logger.debug("aclname: %s user: %s mac:%s", aclname, user, mac)
            base['aauth'][aclname + user + mac] = acllist
            base_acl = base['acls'][aclname]
            i = base_acl.index('authed-rules')
            # insert rules above the authed-rules 'flag'. Add 1 for below it.
            # this may not be included as the reference. but instead inserting each.
            base_acl[i:i] = [{aclname + user + mac: acllist}]

        # 'rotate' filename - filename.bak, filename.bak.1 this is primiarily for logging,
        # to see how users affect the config.

        # write back to filename
        write_yaml(base, filename + '.tmp')
        self.backup_file(filename)
        self.logger.warn('backed up base')
        self.swap_temp_file(filename)
        self.logger.warn('swapped tmp for base')
        return base

    def authenticate(self, username, mac, switch, port, acl_list):
        """Authenticates a username and MAC address on a switch and port.
        Args:
            username (str)
            mac (str): MAC address
            switch (str): Switch that authentication occured on
            port (str): the 'access port' as configured in 'auth.yaml'
            acl_list (list of str): names of acls (in order of highest priority to lowest) to be applied.
        Returns:
            True if rules are found and faucet reloads or already authenticated. False otherwise.
        """
        self.logger.debug('rule-man authenticate')

        # get rules to apply

        rules = self.rule_gen.get_rules(username, 'port_' + switch + '_' + str(port), mac, acl_list)
        if rules is None:
            self.logger.warn('cannot authenticate user: %s, mac: %s no rules found.',
                             username, mac)
            return False
        # update base
        base = self.add_to_base_acls(self.base_filename, rules, username, mac)
        # update faucet
        final = create_faucet_acls(base, self.logger)
        write_yaml(final, self.faucet_acl_filename + '.tmp', True)
        self.backup_file(self.faucet_acl_filename)
        self.swap_temp_file(self.faucet_acl_filename)
        # sighup.
        start_count = self.get_faucet_reload_count()
        self.send_signal(signal.SIGHUP)

        self.logger.info('auth signal sent.')
        for i in range(400):
            end_count = self.get_faucet_reload_count()
            if end_count > start_count:
                self.logger.info('auth - faucet has reloaded.')
                return True
            time.sleep(0.05)
            self.logger.info('auth - waiting for faucet to process sighup config reload. %d', i)
        self.logger.error('auth - faucet did not process sighup within 20 seconds. 0.05 * 400')
        return False

    def get_faucet_reload_count(self):
        """Queries faucet prometheus and finds the number of time faucet has been reloaded.
        Returns:
            number of times faucet has reloaded
        """

        self.logger.debug('getting reload count')
        for i in range(5):
            try:
                txt = auth_app_utils.scrape_prometheus(self.config.prom_url)
                break
            except requests.exceptions.ConnectionError:
                self.logger.warn('connection refused while trying to query prometheus.')
                time.sleep(1)

        self.logger.debug('got reload count')
        for l in txt.splitlines():
            if l.startswith('faucet_config_reload_requests'):
                return int(float(l.split()[1]))
        return 0

    def remove_from_base(self, username, mac):
        """Removes rules that have matching mac= _mac_ and username=_name_
        If both _mac_ and _name_ exist in the rule, both must match
        If only one of _mac_ or _name_ is exist, only one must match.
        Args:
            username (str)
            mac (str): MAC address
        """
        with open(self.base_filename) as f:
            base = yaml.safe_load(f)

        self.logger.info(base)
        remove = []

        if 'aauth' in base:
            for acl in list(base['aauth'].keys()):
                self.logger.debug('aauth acl')
                self.logger.debug(acl)
                for  r in base['aauth'][acl]:
                    rule = r['rule']
                    if '_mac_' in rule and '_name_' in rule:
                        self.logger.debug('mac and name exist')
                        if mac == rule['_mac_'] and \
                                (username is None or \
                                username == rule['_name_']):
                            self.logger.debug('removing based on name and mac')
                            remove.append(acl)
                            break
                    elif '_mac_' in rule and mac == rule['_mac_']:
                        self.logger.debug('removing based on mac')
                        remove.append(acl)
                        break
                    elif '_name_' in rule and username == rule['_name_']:
                        self.logger.warning('removing based on name')
                        remove.append(acl)
                        break
        self.logger.info('remove from auth')
        self.logger.info(remove)
        removed = False
        for aclname in remove:
            del base['aauth'][aclname]
            removed = True

            for port_acl_name, port_acl_list in list(base['acls'].items()):
                for item in port_acl_list:
                    if isinstance(item, dict):
                        if aclname in item:
                            try:
                                base['acls'][port_acl_name].remove(item)
                                removed = True
                            except Exception as e:
                                self.logger.exception(e)

        if removed:
            # only need to write it back if something has actually changed.
            write_yaml(base, self.base_filename + '.tmp')
            self.backup_file(self.base_filename)
            self.swap_temp_file(self.base_filename)

        self.logger.info('updated base')
        self.logger.info(base)
        return base, removed

    def deauthenticate(self, username, mac):
        """Deauthenticates a username or MAC address.
        Args:
            username (str): may be None or '(null)' which is treated as None.
            mac (str): MAC address
        Returns:
            True if a client that is authed has rules removed, or if client is not authed.
            other wise false (faucet fails to reload)
        """
        self.logger.debug('deauthenticate %s %s' % (username, mac))

        self.logger.info('user: {} mac: {} already authenticated removing'.format(username, mac))
        # update base
        base, changed = self.remove_from_base(username, mac)
        # update faucet only if config has changed
        if changed:
            self.logger.info('base has changed. removing from faucet')
            final = create_faucet_acls(base, self.logger)
            write_yaml(final, self.faucet_acl_filename + '.tmp', True)

            self.backup_file(self.faucet_acl_filename)
            self.swap_temp_file(self.faucet_acl_filename)
            # sighup.
            start_count = self.get_faucet_reload_count()
            self.send_signal(signal.SIGHUP)
            self.logger.info('deauth signal sent')
            for i in range(400):
                end_count = self.get_faucet_reload_count()
                if end_count > start_count:
                    self.logger.info('deauth - faucet has reloaded.')
                    return True
                time.sleep(0.05)
                self.logger.info('deauth - waiting for faucet to process sighup config reload on. %d', i)
            self.logger.error('deauth - faucet did not process sighup within 400 * 0.05 seconds.')
            return False
        return True

    @staticmethod
    def backup_file(filename):
        """Backup a file. appends '.bak#' to filename.
        Args:
            filename (str)
        """
        directory = os.path.dirname(filename)
        if directory == '':
            directory = '.'

        filenames = ''.join(os.listdir(directory))
        search_str = os.path.basename(filename) + '.bak'

        matches = re.findall(search_str, filenames)

        i = str(len(matches) + 1)

        # backup old current
        shutil.copy2(filename, filename + '.bak' + i)

    @staticmethod
    def swap_temp_file(filename):
        """Renames the temporary file to become the original.
        Args:
            filename (str)
        """
        os.remove(filename)
        # make new tmp the current.
        os.rename(filename + '.tmp', filename)

    def send_signal(self, signal_type):
        ''' Send a signal to the controller to indicate a change in config file
        Args:
            signal_type: SIGUSR1 for dot1xforwarder, SIGUSR2 for CapFlow
        '''
        if self.config.container_name:
            auth_app_utils.signal_docker_container(self.config.container_name, signal_type)
        else:
            with open(self.config.contr_pid_file, 'r') as pid_file:
                contr_pid = int(pid_file.read())
                os.kill(contr_pid, signal_type)

    def reset_port_acl(self, dp_name, port_num):
        """Reset the port acl back to the original state (where nothing is authenticated)
        Args:
            dp_name (str): name of datapath.
            port_num (int): port number.
        Returns:
            list of MAC addresses (str) that were on the port.
        """
        # TODO optimize the ording of this - do we want to signal faucet asap?
        # find the acl name for that port.
        acl_name = ""
        removed_macs = []
        data = yaml.load(open(self.config.faucet_config_file, 'r'))
        if dp_name in data['dps']:
            self.logger.debug('found dp_name: %s in dps', dp_name)
            if port_num in data['dps'][dp_name]['interfaces']:
                self.logger.debug('found port_num: %d', port_num)
                if 'acl_in' in data['dps'][dp_name]['interfaces'][port_num]:
                    self.logger.debug('can rewrite acl')
                    acl_name = data['dps'][dp_name]['interfaces'][port_num]['acl_in']
                    # find the acl for acl_name in base-original.
                    orig = yaml.load(open(self.config.base_filename + '-orig', 'r'))
                    orig_acl = orig['acls'][acl_name]
                    # copy the original acl over to the current base.
                    base = yaml.load(open(self.config.base_filename, 'r'))

                    base['acls'][acl_name] = orig_acl

                    final = create_faucet_acls(base, self.logger)
                    write_yaml(final, self.faucet_acl_filename + '.tmp', True)
                    self.backup_file(self.faucet_acl_filename)
                    self.swap_temp_file(self.faucet_acl_filename)

                    #removed_macs = self.remove_all_from_authed_dict(dp_name, port_num)

                    write_yaml(base, self.base_filename + '.tmp')
                    self.backup_file(self.base_filename)
                    self.swap_temp_file(self.base_filename)

                    # sighup.
                    start_count = self.get_faucet_reload_count()
                    self.send_signal(signal.SIGHUP)
                    self.logger.info('reset acl signal sent.')
                    for i in range(400):
                        end_count = self.get_faucet_reload_count()
                        if end_count > start_count:
                            self.logger.info('reset acl - faucet has reloaded.')
                            return removed_macs
                        time.sleep(0.05)
                        self.logger.info('reset - waiting for faucet to process sighup config reload. %d', i)
                    self.logger.error('reset - faucet did not process sighup within 20 seconds. 0.05 * 400')

                    # send signal.

        return removed_macs
if __name__ == '__main__':
    main()
