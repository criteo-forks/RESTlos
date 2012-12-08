#!/usr/bin/env python
# -*- coding: UTF-8 -*-

from flask import Flask, request, render_template, jsonify, abort
from flask import Request, Response
from flask.views import MethodView

from werkzeug.exceptions import HTTPException, InternalServerError
from werkzeug.exceptions import default_exceptions, BadRequest

from utils import config
from utils.authentication import Authentify

from pynag import Model
from json import dumps
from cgi import escape

import logging
import logging.config

#TODO: README.md
#TODO: Sample config.json
#TODO: MAM config schnipsel mit debug logfile

class JSONHTTPException(HTTPException):
    """ JSONHTTPException: this exception provides a detailed error message
    if a json parsing error occures. More helpful that just the standard 400
    message which tells you that "something" is wrong
    """

    def get_body(self, environ):
        return dumps(dict(description=self.get_description(environ)))

    def get_headers(self, environ):
        return [('Content-Type', 'application/json')]


class JSONBadRequest(JSONHTTPException, BadRequest):
    description = ('The browser (or proxy) sent a request that this server could not understand.')


class CustomRequestClass(Request):
    def on_json_loading_failed(self, e):
        raise JSONBadRequest(str(e))


class ApiEndpoints(dict):
    """ ApiEndpoints: provides a dictionary for the available api endpoints.
    it also defines unique keys for the available nagios objects and some
    convenient functions for retrieving unique keys or validating object
    attributes.
    """

    def __init__(self):
        # create a map of valid endpoints/arguments
        for endpoint in Model.all_attributes.object_definitions.keys():
            self[endpoint] = Model.all_attributes.object_definitions[endpoint].keys()
            self[endpoint] += Model.all_attributes.object_definitions["any"]
        del self["any"]

        # set endpoint keys
        self.endpoint_keys = {
            'hostgroup':'hostgroup_name',
            'hostextinfo':'host_name',
            'host':'host_name',
            'service':'service_description',
            'servicegroup':'servicegroup_name',
            'contact':'contact_name',
            'contactgroup':'contactgroup_name',
            'timeperiod':'timeperiod_name',
            'command':'command_name',
        }

    def get_unique_key(self, endpoint):
        return self.endpoint_keys[endpoint]

    def validate(self, endpoint, data={}):
        for attr in data.keys():
            if not attr.startswith('_') and attr not in self[endpoint]:
                abort(404, "unknown attribute: %s" % (attr, ))


class NagiosControlView(MethodView):
    """
    NagiosControlView: Provides a view function which is registered as an api
    endpoint as well. This '/control' endpoint provides convenient functions
    like reloading the core or verify the configuration
    """

    #decorators = [Authentify(AuthLDAP('ldap.example.com'))]
    decorators = [Authentify()]

    def __init__(self, *args, **kwargs):
        MethodView.__init__(self, *args, **kwargs)

        try:
            self.command_file = Model.Control.Command.find_command_file(config['nagios_main_cfg'])
        except Exception, err:
            abort(500, 'unable to locate command file: %s' % (str(err), ))

        self.arguments = ['verify', 'restart']

    def _verify(self):
        return Model.Control.Command.restart_program(command_file=self.command_file)

    def _restart(self):
        logging.warn("%s triggered the restart command" % (request.authorization.username), )
        return Model.Control.Command.restart_program(command_file=self.command_file)

    def post(self):
        if len(request.args.keys()) != 1:
            abort(400, 'control endpoint accepts exactly ONE argument')

        action = request.args.keys()[0]

        if action not in self.arguments:
            abort(400, 'invalid argument: %s' % (escape(action), ))

        try:
            result = getattr(self,'_' + action)()
        except Exception, err:
            abort(500, 'unable to execute action %s: %s' % (action, str(err)))
        else:
            return jsonify(result=result if result else "successfully sent command to command file")


class NagiosObjectView(MethodView):
    """
    NagiosObjectView: Wraps the flask MethodViews around the wonderful 
    pynag module providing a full featured RESTfull api for managing 
    Nagios/Icinga Configurations
    """

    #decorators = [Authentify(AuthLDAP('ldap.example.com'))]
    decorators = [Authentify()]

    def __init__(self, *args, **kwargs):
        MethodView.__init__(self, *args, **kwargs)
        Model.cfg_file=config['nagios_main_cfg']
        Model.pynag_directory=config['output_dir']

        self.username = request.authorization.username
        self.endpoint = request.path.lstrip('/')
        self.endpoints = ApiEndpoints()

    def _json_message(self, msg, code=200):
        return Response(jsonify(message=msg), code)

    def _summary(self, results):
        return {
            "succeeded": len([r for r in results if r.has_key(200) ]), 
            "failed": len([r for r in results if not r.has_key(200) ]), 
            "total": len(results)
        }

    def get(self):
        self.endpoints.validate(self.endpoint, request.args)
        endpoint_objects = getattr(Model, self.endpoint.capitalize()).objects

        # build the "contains" query string
        query = dict([ (key + '__contains', value) for key, value in request.args.iteritems() ])

        result = [ obj['meta']['defined_attributes'] for obj in endpoint_objects.filter(**query)]
        return Response(dumps(result, indent=None if request.is_xhr else 2), mimetype='application/json')

    def delete(self):
        self.endpoints.validate(self.endpoint, request.args)
        endpoint_objects = getattr(Model, self.endpoint.capitalize()).objects

        # build the "contains" query string
        query = dict([ (key + '__contains', value) for key, value in request.args.iteritems() ])

        objects = endpoint_objects.filter(**query)
        unique_key = self.endpoints.get_unique_key(self.endpoint)
        results = []
        for obj in objects:
            try:
                obj.delete()
            except Exception, err:
                results.append({ 500: "unable to delete %s object %s: %s" % (self.endpoint, obj[unique_key], str(err)) })
                logging.debug("%s failed to delete %s object %s: %s" % (self.username, self.endpoint, obj[unique_key], str(err)))
            else:
                results.append({ 200: "successfully deleted %s object: %s" % (self.endpoint, obj[unique_key]) })
                logging.info("%s deleted %s object: %s" % (self.username, self.endpoint, obj[unique_key]))

        summary = self._summary(results)
        logging.warn("%s deleted %d %s objects (out of %d requested)" % (self.username, summary['succeeded'], self.endpoint, summary['total']))
        return jsonify(results=results, summary=summary)

    def post(self):
        data = request.json

        if not data:
            return self._json_message('no json received. you need to set your content-type to application/json.')

        if type(data) == list:
            results = map(self._save_or_update, data)
        else:
            results = [self._save_or_update(data)]

        summary = self._summary(results)
        logging.warn("%s stored %d %s objects (out of %d requested)" % (self.username, summary['succeeded'], self.endpoint, summary['total']))
        return jsonify(results=results, summary=summary)

    def _save_or_update(self, item):
            # does this object already exist
            unique_key = self.endpoints.get_unique_key(self.endpoint)
            if unique_key in item.keys():
                query = { unique_key: item[unique_key] }
                endpoint_object = getattr(Model, self.endpoint.capitalize()).objects.filter(**query)

                if endpoint_object:
                    endpoint_object = endpoint_object[0]
                else:
                    endpoint_object = getattr(Model, self.endpoint.capitalize())()
            else:
                return { 500: 'required key for %s object not set: %s' % (self.endpoint, unique_key) }

            for key, value in item.iteritems():
                if not key.startswith('_') and key not in self.endpoints[self.endpoint]:
                    return { 500: "invalid %s attribute: %s" % (self.endpoint, key) }

                endpoint_object.set_attribute(key, value)

            try:
                endpoint_object.save()
            except Exception, err:
                logging.debug("%s failed to store %s object %s: %s" % (self.username, self.endpoint, item[unique_key], str(err)))
                return { 500: 'unable to save %s object %s: %s' % (self.endpoint, item[unique_key], str(err)) }

            logging.info("%s stored %s object %s" % (self.username, self.endpoint, item[unique_key]))
            return { 200: "successfully stored %s object: %s" % (self.endpoint, item[unique_key]) }

class NagiosAPI(Flask):
    """
    APIEndpoints: Handles the flask app, registers endpoints and wrapping 
    the run() function for development testing.

    @app: Flask app where the enpoints sould be registered at
    """

    def __init__(self, name):
        Flask.__init__(self, name)

        logging.config.dictConfig(config['logging'])

        self.request_class = CustomRequestClass
        self.endpoints = ApiEndpoints()

        self._register_endpoints()
        self._register_error_handler()
        self._register_help_handler()

    def _error_handler(self, err):
        if not isinstance(err, HTTPException):
            err = InternalServerError(description="Something went wrong. RUN!")

        if request.content_type=='application/json':
            response = jsonify(message=str(err))
        else:
            response = Response(render_template('error.html', err=err))

        response.status_code = err.code

        if err.code == 401:
            response.headers['WWW-Authenticate'] = 'Basic realm="Login Required"'

        return response

    def _help(self):
        if request.content_type=='application/json':
            return jsonify(endpoints=self.endpoints)
        else:
            return render_template('help.html', endpoints=self.endpoints)

    def _register_help_handler(self):
        for endpoint, name in [('/', 'index'), ('/help', 'help')]:
            self.add_url_rule(endpoint, name, self._help)

    def _register_error_handler(self):
        for code in default_exceptions.iterkeys():
            self.error_handler_spec[None][code] = self._error_handler

    def _register_endpoints(self):
        for endpoint in self.endpoints.keys():
            self.add_url_rule('/' + endpoint, view_func=NagiosObjectView.as_view(endpoint))
        self.add_url_rule('/control', view_func=NagiosControlView.as_view('control'))


if __name__ == '__main__':
    app = NagiosAPI(__name__)
    #app.run(debug=True, port=config['port'])
    app.run(debug=False)