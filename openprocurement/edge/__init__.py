# -*- coding: utf-8 -*-
"""Main entry point
"""
if 'test' not in __import__('sys').argv[0]:
    import gevent.monkey
    gevent.monkey.patch_all()
import os
from couchdb import Server as CouchdbServer, Session
from couchdb.http import Unauthorized, extract_credentials
from logging import getLogger
from openprocurement.api.auth import AuthenticationPolicy, authenticated_role, check_accreditation
from openprocurement.api.design import sync_design
from openprocurement.api.utils import forbidden, add_logging_context, extract_tender, request_params, set_renderer, beforerender, route_prefix, set_logging_context
from openprocurement.edge.utils import extract_tender, extract_auction, extract_contract, extract_plan
try:
    import openprocurement.auctions.core as auctions_core
    from openprocurement.auctions.core.design import add_design as add_auction_design
except ImportError:
     auctions_core = None
try:
    import openprocurement.contracting.api as contracting
    from openprocurement.contracting.api.design import add_design as add_contract_design
except ImportError:
     contracting = None
try:
    import openprocurement.planning.api as planning
    from openprocurement.planning.api.design import add_design as add_plan_design
except ImportError:
     planning = None
from pbkdf2 import PBKDF2
from pyramid.authorization import ACLAuthorizationPolicy as AuthorizationPolicy
from pyramid.config import Configurator
from pyramid.events import NewRequest, BeforeRender, ContextFound
from pyramid.renderers import JSON, JSONP
from pyramid.settings import asbool

LOGGER = getLogger("{}.init".format(__name__))
SECURITY = {u'admins': {u'names': [], u'roles': ['_admin']}, u'members': {u'names': [], u'roles': ['_admin']}}
VALIDATE_DOC_ID = '_design/_auth'
VALIDATE_DOC_UPDATE = """function(newDoc, oldDoc, userCtx){
    if(newDoc._deleted && newDoc.tenderID) {
        throw({forbidden: 'Not authorized to delete this document'});
    }
    if(userCtx.roles.indexOf('_admin') !== -1 && newDoc._id.indexOf('_design/') === 0) {
        return;
    }
    if(userCtx.name === '%s') {
        return;
    } else {
        throw({forbidden: 'Only authorized user may edit the database'});
    }
}"""


class Server(CouchdbServer):
    _uuid = None

    @property
    def uuid(self):
        """The uuid of the server.

        :rtype: basestring
        """
        if self._uuid is None:
            _, _, data = self.resource.get_json()
            self._uuid = data['uuid']
        return self._uuid


def main(global_config, **settings):
    config = Configurator(
        autocommit=True,
        settings=settings,
        authentication_policy=AuthenticationPolicy(settings['auth.file'], __name__),
        authorization_policy=AuthorizationPolicy(),
        route_prefix=route_prefix(settings),
    )
    config.include('pyramid_exclog')
    config.include("cornice")
    config.add_forbidden_view(forbidden)
    config.add_request_method(request_params, 'params', reify=True)
    config.add_request_method(authenticated_role, reify=True)
    config.add_request_method(extract_tender, 'tender', reify=True)
    config.add_request_method(check_accreditation)
    config.add_renderer('prettyjson', JSON(indent=4))
    config.add_renderer('jsonp', JSONP(param_name='opt_jsonp'))
    config.add_renderer('prettyjsonp', JSONP(indent=4, param_name='opt_jsonp'))
    config.add_subscriber(add_logging_context, NewRequest)
    config.add_subscriber(set_logging_context, ContextFound)
    config.add_subscriber(set_renderer, NewRequest)
    config.add_subscriber(beforerender, BeforeRender)
    config.scan("openprocurement.edge.views.spore")
    config.scan("openprocurement.edge.views.health")
    config.scan("openprocurement.edge.views.tenders")

    if auctions_core:
        config.add_request_method(extract_auction, 'auction', reify=True)
        config.scan("openprocurement.edge.views.auctions")
        add_auction_design()

    if contracting:
        config.add_request_method(extract_contract, 'contract', reify=True)
        config.scan("openprocurement.edge.views.contracts")
        add_contract_design()

    if planning:
        config.add_request_method(extract_plan, 'plan', reify=True)
        config.scan("openprocurement.edge.views.plans")
        add_plan_design()

    # CouchDB connection
    db_name = os.environ.get('DB_NAME', settings['couchdb.db_name'])
    server = Server(settings.get('couchdb.url'), session=Session(retry_delays=range(10)))
    if 'couchdb.admin_url' not in settings and server.resource.credentials:
        try:
            server.version()
        except Unauthorized:
            server = Server(extract_credentials(settings.get('couchdb.url'))[0])
    config.registry.couchdb_server = server
    if 'couchdb.admin_url' in settings and server.resource.credentials:
        aserver = Server(settings.get('couchdb.admin_url'), session=Session(retry_delays=range(10)))
        config.registry.admin_couchdb_server = aserver
        users_db = aserver['_users']
        if SECURITY != users_db.security:
            LOGGER.info("Updating users db security", extra={'MESSAGE_ID': 'update_users_security'})
            users_db.security = SECURITY
        username, password = server.resource.credentials
        user_doc = users_db.get('org.couchdb.user:{}'.format(username), {'_id': 'org.couchdb.user:{}'.format(username)})
        if not user_doc.get('derived_key', '') or PBKDF2(password, user_doc.get('salt', ''), user_doc.get('iterations', 10)).hexread(int(len(user_doc.get('derived_key', '')) / 2)) != user_doc.get('derived_key', ''):
            user_doc.update({
                "name": username,
                "roles": [],
                "type": "user",
                "password": password
            })
            LOGGER.info("Updating edge db main user", extra={'MESSAGE_ID': 'update_edge_main_user'})
            users_db.save(user_doc)
        security_users = [username, ]
        if 'couchdb.reader_username' in settings and 'couchdb.reader_password' in settings:
            reader_username = settings.get('couchdb.reader_username')
            reader = users_db.get('org.couchdb.user:{}'.format(reader_username), {'_id': 'org.couchdb.user:{}'.format(reader_username)})
            if not reader.get('derived_key', '') or PBKDF2(settings.get('couchdb.reader_password'), reader.get('salt', ''), reader.get('iterations', 10)).hexread(int(len(reader.get('derived_key', '')) / 2)) != reader.get('derived_key', ''):
                reader.update({
                    "name": reader_username,
                    "roles": ['reader'],
                    "type": "user",
                    "password": settings.get('couchdb.reader_password')
                })
                LOGGER.info("Updating edge db reader user", extra={'MESSAGE_ID': 'update_edge_reader_user'})
                users_db.save(reader)
            security_users.append(reader_username)
        if db_name not in aserver:
            aserver.create(db_name)
        db = aserver[db_name]
        SECURITY[u'members'][u'names'] = security_users
        if SECURITY != db.security:
            LOGGER.info("Updating edge db security", extra={'MESSAGE_ID': 'update_edge_security'})
            db.security = SECURITY
        auth_doc = db.get(VALIDATE_DOC_ID, {'_id': VALIDATE_DOC_ID})
        if auth_doc.get('validate_doc_update') != VALIDATE_DOC_UPDATE % username:
            auth_doc['validate_doc_update'] = VALIDATE_DOC_UPDATE % username
            LOGGER.info("Updating edge db validate doc", extra={'MESSAGE_ID': 'update_edge_validate_doc'})
            db.save(auth_doc)
        # sync couchdb views
        sync_design(db)
        db = server[db_name]
    else:
        if db_name not in server:
            server.create(db_name)
        db = server[db_name]
        # sync couchdb views
        sync_design(db)
    config.registry.db = db

    config.registry.server_id = settings.get('id', '')
    config.registry.health_threshold = float(settings.get('health_threshold', 99))
    config.registry.update_after = asbool(settings.get('update_after', True))
    return config.make_wsgi_app()
