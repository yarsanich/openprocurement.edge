# -*- coding: utf-8 -*-
from gevent import monkey
monkey.patch_all()

try:
    import urllib3.contrib.pyopenssl
    urllib3.contrib.pyopenssl.inject_into_urllib3()
except ImportError:
    pass

import logging
import logging.config
import os
import psutil
import argparse
import uuid
from yaml import load
from urlparse import urljoin, urlparse
from couchdb import Server, Session, ResourceNotFound
from openprocurement_client.sync import get_resource_items
from openprocurement_client.exceptions import InvalidResponse, RequestFailed
from openprocurement_client.client import TendersClient as APIClient
from openprocurement.edge.collector import LogsCollector
import errno
from socket import error
from requests.exceptions import ConnectionError, MissingSchema
import gevent.pool
from gevent import spawn, sleep, idle
from gevent.queue import Queue, Empty
from datetime import datetime
from .workers import ResourceItemWorker

logger = logging.getLogger(__name__)

defaults_workers = {
    'resourse': 'tenders',
    'client_inc_step_timeout': 0.1,
    'client_dec_step_timeout': 0.02,
    'drop_threshold_client_cookies': 2,
    'worker_sleep': 5,
    'retry_default_timeout': 5,
    'retries_count': 10,
    'queue_timeout': 3,
}

defaults = {
    'workers_min': 1,
    'workers_max': 3,
    'retry_workers_min': 1,
    'retry_workers_max': 2,
    'filter_workers_count': 1,
    'watch_interval': 10,
    'user_agent': 'edge_' + defaults_workers['resource'] + '.client',
    'couch_url': 'http://127.0.0.1:5984',
    'public_db': 'edge_db',
    'logs_db': 'logs_db',
    'resource_items_limit': 100,
    'resource_items_queue_size': 102,
    'retry_resource_items_queue_size': -1,
    'workers_inc_threshold': 90,
    'workers_dec_threshold': 30,
    'queues_controller_timeout': 60
}



class DataBridgeConfigError(Exception):
    pass


class EdgeDataBridge(object):

    """Edge Bridge"""

    def __init__(self, config):
        super(EdgeDataBridge, self).__init__()
        self.config = config
        self.workers_config = {}
        self.log_dict = {}
        self.api_host = self.config_get('resources_api_server')
        self.api_version = self.config_get('resources_api_version')
        self.retrievers_params = self.config_get('retrievers_params')

        #config init
        for key in defaults:
            setattr(self,key, self.config_get(key) or defaults[key])
        # Workers settings
        for key in defaults_workers:
            self.workers_config[key] = self.config_get(key) or defaults_workers[key]

        self.workers_pool = gevent.pool.Pool(self.workers_max)
        self.retry_workers_pool = gevent.pool.Pool(self.retry_workers_max)
        self.filter_workers_pool = gevent.pool.Pool(self.filter_workers_count)
        if self.resource_items_queue_size == -1:
            self.resource_items_queue = Queue()
        else:
            self.resource_items_queue = Queue(self.resource_items_queue_size)
        self.api_clients_queue = Queue()
        if self.retry_resource_items_queue_size == -1:
            self.retry_resource_items_queue = Queue()
        else:
            self.retry_resource_items_queue = Queue(self.retry_resource_items_queue_size)

        self.process = psutil.Process(os.getpid())

        # Variables for statistic
        for key in ('not_actual_docs_count', 'update_documents', 'save_documents',
                    'add_to_retry', 'droped', 'skiped',
                    'add_to_resource_items_queue',
                    'exceptions_count', 'not_found_count'):
            self.log_dict[key] = 0

        if self.api_host != '' and self.api_host is not None:
            api_host = urlparse(self.api_host)
            if api_host.scheme == '' and api_host.netloc == '':
                raise DataBridgeConfigError(
                    'Invalid \'tenders_api_server\' url.')
        else:
            raise DataBridgeConfigError('In config dictionary empty or missing'
                                        ' \'tenders_api_server\'')

        server = Server(self.couch_url, session=Session(retry_delays=range(10)))

        try:
            if self.db_name not in server:
                self.db = server.create(self.db_name)
            else:
                self.db = server[self.db_name]
        except error as e:
            logger.error('Database error: {}'.format(e.message))
            raise DataBridgeConfigError(e.strerror)

        collector_config = {
            'main': {
                'storage': 'couchdb',
                'couch_url': self.couch_url,
                'log_db': self.log_db_name
            }
        }
        self.logger = LogsCollector(collector_config)

    def config_get(self, name):
        try:
            return self.config.get('main').get(name)
        except AttributeError as e:
            raise DataBridgeConfigError('In config dictionary missed section'
                                        ' \'main\'')


    def create_api_client(self):
        client_user_agent = self.user_agent + '/' + uuid.uuid4().hex
        timeout = 0
        while 1:
            try:
                api_client = APIClient(host_url=self.api_host,
                                       user_agent=client_user_agent,
                                       api_version=self.api_version,
                                       key='',
                                       resource=self.workers_config['resource'])
                self.api_clients_queue.put({
                    'client': api_client,
                    'request_interval': 0})
                logger.info('Started api_client {}'.format(
                    api_client.session.headers['User-Agent']))
                break
            except RequestFailed as e:
                self.log_dict['exceptions_count'] += 1
                logger.error('Failed start api_client with status code {}'.format(
                    e.status_code
                ))
                timeout += 0.1
                sleep(timeout)

    def fill_api_clients_queue(self):
        while self.api_clients_queue.qsize() < self.workers_min:
            self.create_api_client()

    def fill_resource_items_queue(self):
        for resource_item in get_resource_items(
            host=self.api_host, version=self.api_version, key='',
            extra_params={'mode': '_all_', 'limit': self.resource_items_limit},
            resource=self.workers_config['resource'], retrievers_params=self.retrievers_params):
            if self.resource_items_filter(resource_item['id'],
                                     resource_item['dateModified']):
                self.resource_items_queue.put({
                    'id': resource_item['id'],
                    'dateModified': resource_item['dateModified']})
                self.log_dict['add_to_resource_items_queue'] += 1
            else:
                self.log_dict['skiped'] += 1

    def resource_items_filter(self, r_id, r_date_modified):
        try:
            local_document = self.db.get(r_id)
            if local_document:
                if local_document['dateModified'] < r_date_modified:
                    return True
                else:
                    return False
            else:
                return True
        except Exception as e:
            logger.error('Filter error: Error while getting {} {} from'
                         ' couchdb: {}'.format(self.workers_config['resource'][:-1],
                                               r_id, e.message))
            return True

    def reset_log_counters(self):
        self.log_dict['not_actual_docs_count'] = 0
        self.log_dict['add_to_retry'] = 0
        self.log_dict['droped'] = 0
        self.log_dict['update_documents'] = 0
        self.log_dict['save_documents'] = 0
        self.log_dict['skiped'] = 0
        self.log_dict['not_found_count'] = 0
        self.log_dict['exceptions_count'] = 0
        self.log_dict['add_to_resource_items_queue'] = 0

    def bridge_stats(self):
        return dict(
            time=datetime.now().isoformat(),
            resource_items_queue_size=self.resource_items_queue.qsize(),
            retry_resource_items_queue_size=self.retry_resource_items_queue.qsize(),
            workers_count=self.workers_max - self.workers_pool.free_count(),
            filter_workers_count=self.filter_workers_count - self.filter_workers_pool.free_count(),
            retry_workers_count=self.retry_workers_max - self.retry_workers_pool.free_count(),
            free_api_clients=self.api_clients_queue.qsize(),
            save_documents=self.log_dict['save_documents'],
            update_documents=self.log_dict['update_documents'],
            add_to_retry=self.log_dict['add_to_retry'],
            droped=self.log_dict['droped'],
            skiped=self.log_dict['skiped'],
            rss=self.process.memory_info().rss/1024/1024,
            vms=self.process.memory_info().vms/1024/1024,
            exceptions_count=self.log_dict['exceptions_count'],
            not_found_count=self.log_dict['not_found_count'],
            not_actual_docs_count=self.log_dict['not_actual_docs_count'],
            add_to_resource_items_queue=self.log_dict['add_to_resource_items_queue'],
            resource=self.workers_config['resource']
        )

    def queues_controller(self):
        while True:
            if self.workers_pool.free_count() > 0 and (self.resource_items_queue.qsize() > int((self.resource_items_queue_size / 100) * self.workers_inc_threshold)):
                self.create_api_client()
                w = ResourceItemWorker.spawn(self.api_clients_queue,
                                             self.resource_items_queue,
                                             self.db, self.workers_config,
                                             self.retry_resource_items_queue,
                                             self.log_dict)
                self.workers_pool.add(w)
            elif self.resource_items_queue.qsize() < int((self.resource_items_queue_size / 100) * self.workers_dec_threshold):
                if len(self.workers_pool) > self.workers_min:
                    wi = self.workers_pool.greenlets.pop()
                    wi.shutdown()
            filled_resource_items_queue = int(self.resource_items_queue.qsize()/(self.resource_items_queue_size / 100))
            logger.info('Resource items queue filled on {} %'.format(filled_resource_items_queue))
            filled_retry_resource_items_queue = int(self.retry_resource_items_queue.qsize()/(self.retry_resource_items_queue_size / 100))
            logger.info('Retry resource items queue filled on {} %'.format(filled_retry_resource_items_queue))
            sleep(self.queues_controller_timeout)


    def gevent_watcher(self):
        spawn(self.logger.save, self.bridge_stats())
        self.reset_log_counters()
        for i in xrange(0, self.filter_workers_pool.free_count()):
            self.filter_workers_pool.spawn(self.fill_resource_items_queue)
        if len(self.workers_pool) < self.workers_min:
            for i in xrange(0, (self.workers_min - len(self.workers_pool))):
                w = ResourceItemWorker.spawn(self.api_clients_queue,
                                             self.resource_items_queue,
                                             self.db, self.workers_config,
                                             self.retry_resource_items_queue,
                                             self.log_dict)
                self.workers_pool.add(w)
        if len(self.retry_workers_pool) < self.retry_workers_min:
            for i in xrange(0, self.retry_workers_min - len(self.retry_workers_pool)):
                self.create_api_client()
                w = ResourceItemWorker.spawn(self.api_clients_queue,
                                             self.retry_resource_items_queue,
                                             self.db, self.workers_config,
                                             self.retry_resource_items_queue,
                                             self.log_dict)
                self.retry_workers_pool.add(w)

    def run(self):
        logger.info('Start Edge Bridge',
                    extra={'MESSAGE_ID': 'edge_bridge_start_bridge'})
        logger.info('Start data sync...',
                    extra={'MESSAGE_ID': 'edge_bridge__data_sync'})
        self.fill_api_clients_queue()
        self.filter_workers_pool.spawn(self.fill_resource_items_queue)
        spawn(self.queues_controller)
        while 1:
            self.gevent_watcher()
            sleep(self.watch_interval)


def main():
    parser = argparse.ArgumentParser(description='---- Edge Bridge ----')
    parser.add_argument('config', type=str, help='Path to configuration file')
    params = parser.parse_args()
    if os.path.isfile(params.config):
        with open(params.config) as config_file_obj:
            config = load(config_file_obj.read())
        logging.config.dictConfig(config)
        EdgeDataBridge(config).run()


##############################################################

if __name__ == "__main__":
    main()
