import os
import json
import psutil
import socket
import argparse
from time import sleep
from configurationparser import ConfigurationParser
from collections import OrderedDict
from shutil import copyfile
from shutil import rmtree
from utility import join
from utility import file_get_contents
from utility import start_background_proc
from utility import run_continue
from utility import log_file
from utility import create_file
from utility import tail


class Node:

    def __init__(self, name, path):
        self.config = ConfigurationParser()
        self.path = os.path.abspath(path)
        self.name = name
        self.config.read(join(path, 'config.ini'))
        self.pid = self.get_pid()

    def is_running(self):
        try:
            pid = self.get_pid()
            if pid != -1:
                return psutil.pid_exists(pid)
            return False
        except OSError as e:
            print(e)
            return False

    def get_pid(self):
        path = join(self.path, 'node.pid')
        if os.path.isfile(path):
            pid = int(file_get_contents(path))
            self.pid = pid
            return pid
        return -1

    def start(self, delay_time=1.0):
        if not os.path.isdir(self.path):
            os.makedirs(self.path)
        cmd = 'nodeos --config-dir %s --genesis-json %s --delete-all-blocks'
        genesis_dir = join(self.path, 'genesis.json')
        start_background_proc(cmd % (self.path, genesis_dir), log_file(join(self.path, 'stderr.txt')),
                              join(self.path, 'node.pid'))
        sleep(delay_time)

    def restart(self):
        if self.is_running():
            self.stop()
        if os.path.isdir(self.path):
            cmd = 'nodeos --config-dir %s --hard-replay-blockchain'
            start_background_proc(cmd % self.path, log_file(join(self.path, 'stderr.txt')), join(self.path, 'node.pid'))
            sleep(1.0)
        else:
            print('nodeos folder path: %s does not exist' % self.path)

    def stop(self):
        try:
            pid = self.get_pid()
            if pid != -1 and self.is_running():
                p = psutil.Process(pid)
                p.terminate()
        except OSError as e:
            print(e)

    def show_output(self):
        tail(join(self.path, 'stderr.txt'))

    def get_ports(self):
        p2p = self.config.get('p2p-listen-endpoint')
        p2p = int(p2p[p2p.index(':') + 1:len(p2p) + 1])
        http = self.config.get('http-server-address')
        http = int(http[http.index(':') + 1:len(http) + 1])
        return [p2p, http]

    def get_endpoints(self):
        return self.config.get('p2p-server-address')

    def set_peers(self, peers):
        tmp = peers.copy()
        self.remove_self(tmp)
        self.config.append('p2p-peer-address', tmp)
        self.config.write(join(self.path, 'config.ini'))

    def remove_self(self, peers):
        if self.name in peers:
            del peers[self.name]

    def get_info(self):
        return {'name': self.name, 'pid': self.get_pid(), 'path': self.path, 'ports': self.get_ports()}

    def __str__(self):
        info = self.get_info()
        return json.dumps(info, indent=4, sort_keys=True)


class NodeFactory:

    def __init__(self, working, parent, nodeos, wallet):
        self.folder_scheme = 'tn-'
        self.nodeos_dir = nodeos
        self.parent_dir = parent
        self.working_dir = working
        self.config_dir = join(parent, 'config/nodes.json')
        self.state = json.loads(file_get_contents(self.config_dir))
        if 'nodes' not in self.state:
            self.state['nodes'] = {}
        self.wallet = wallet

    def create_sig_provider(self, keypair):
        return '%s=KEY:%s' % (keypair.public, keypair.private)

    def edit_new_genesis(self, public):
        j = json.loads(file_get_contents(join(self.parent_dir, 'config/genesis.json')))
        j['initial_key'] = public
        create_file(join(self.parent_dir, 'config/genesis.json'), json.dumps(j))

    def start_full(self, path, p2p_address, http_port, p2p_port):
        try:
            nodepath = join(path, self.folder_scheme + 'full')
            if not os.path.isdir(nodepath):
                os.makedirs(nodepath)
            os.chdir(nodepath)
            config = ConfigurationParser()
            config.read(join(self.parent_dir, 'config/template_config.ini'))
            config.set('http-server-address', '0.0.0.0:' + http_port)
            config.set('p2p-listen-endpoint', '0.0.0.0:' + p2p_port)
            config.set('p2p-server-address', '%s:%s' % (p2p_address, p2p_port))
            config.set('producer-name', 'eosio')
            pair = self.wallet.create_import()
            self.edit_new_genesis(pair.public)
            config.set('signature-provider', self.create_sig_provider(pair))
            plugins = ['eosio::http_plugin', 'eosio::chain_plugin', 'eosio::chain_api_plugin',
                       'eosio::history_plugin',
                       'eosio::history_api_plugin', 'eosio::net_plugin', 'eosio::net_api_plugin',
                       'eosio::producer_plugin']
            config.append('plugin', plugins)
            config.write(join(nodepath, 'config.ini'))
            copyfile(join(self.parent_dir, 'config/genesis.json'), join(nodepath, "genesis.json"))
            node = Node(self.folder_scheme + 'full', nodepath)
            node.start(3.0)
            self.update_node_state(node)
        except FileNotFoundError as e:
            print(e)

    def create_producer_node(self, account, path, p2p_address):
        try:
            nodepath = join(path, self.folder_scheme + account.name)
            if not os.path.isdir(nodepath):
                os.makedirs(nodepath)
            os.chdir(nodepath)
            config = ConfigurationParser()
            config.read(join(self.parent_dir, 'config/template_config.ini'))
            config.set('http-server-address', '0.0.0.0:' + self.get_open_port())
            config.set('p2p-listen-endpoint', '0.0.0.0:' + self.get_open_port())
            config.set('p2p-server-address', '%s:%s' % (p2p_address, self.get_open_port()))
            config.set('producer-name', account.name)
            config.set('signature-provider', self.create_sig_provider(self.wallet.create_import()))
            plugins = ['eosio::producer_plugin']
            config.append('plugin', plugins)
            config.write(join(nodepath, 'config.ini'))
            copyfile(join(self.parent_dir, 'config/genesis.json'), join(nodepath, "genesis.json"))
            node = Node(self.folder_scheme + account.name, nodepath)
            return node
        except FileNotFoundError as e:
            print(e)

    def start_producers_by_account(self, accounts, path):
        nodes = []
        endpoints = {}
        for a in accounts:
            node = self.create_producer_node(a, '0.0.0.0')
            endpoints[a.name] = node.get_endpoints()
            nodes.append(node)

        for n in nodes:
            n.set_peers(endpoints)
            n.start()
            self.update_node_state(n)

    def get_status(self, name):
        n = self.get_node_from_state(name)
        status = 'OFFLINE'
        if n.is_running():
            status = 'ONLINE'
        print('Node %s is %s' % (n.name, status))

    def get_nodes(self):
        if 'nodes' not in self.state:
            self.state['nodes'] = {}
        return json.dumps(self.state['nodes'], indent=4, sort_keys=True)

    def get_node_from_state(self, name):
        if 'nodes' not in self.state:
            self.state['nodes'] = {}
        if name in self.state['nodes']:
            n = self.state['nodes'][name]
            return Node(n['name'], n['path'])
        else:
            raise ValueError('Node %s does not exist in nodes.json')

    def get_all_nodes_from_state(self):
        if 'nodes' not in self.state:
            self.state['nodes'] = {}
        nodes = []
        for name in self.state['nodes']:
            nodes.append(self.get_node_from_state(name))
        return nodes

    def get_open_port(self):
        port = self.last_found_port()
        while not self.is_port_unclaimed(port):
            port = self.get_port_from_range(port)
        self.set_last_found_port(port + 1)
        return port

    def is_port_unclaimed(self, port):
        if 'nodes' not in self.state:
            self.state['nodes'] = {}

        for node in self.state['nodes']:
            for node_port in node['ports']:
                if node_port == port:
                    return False
        return True

    # TODO: See if there are any special update operations that need to be resolved
    def update_node_state(self, node):
        if 'nodes' not in self.state:
            self.state['nodes'] = {}

        if node.name in self.state['nodes']:
            self.state['nodes'][node.name] = node.get_info()
        else:
            self.state['nodes'][node.name] = node.get_info()

    def get_port_from_range(self, floor):
        for port in range(floor, 10000):
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            result = sock.connect_ex(('0.0.0.0', port))
            if result == 0:
                print("Port {}: 	 Open".format(port))
                return port
            sock.close()

    def find_open_port(self):
        sock = socket.socket()
        sock.bind(('', 0))
        _, port = sock.getsockname()
        return port

    def set_last_found_port(self, port):
        self.state['last-found-port'] = port

    def last_found_port(self):
        if 'last-found-port' not in self.state:
            self.state['last-found-port'] = 7000
        return self.state['last-found-port']

    def save(self):
        with open(self.config_dir, 'w') as f:
            j = json.dumps(self.state)
            f.write(j)
            f.close()

    def clear_state(self):
        self.state = {}

    def delete_all_nodes(self):
        try:
            nodes = self.get_all_nodes_from_state()
            for n in nodes:
                print('Stopping node %s...' % n.name)
                n.stop()
                self.get_status(n.name)
                if os.path.isdir(n.path):
                    rmtree(n.path)
            self.clear_state()
            self.save()
        except FileNotFoundError as e:
            print(e)
