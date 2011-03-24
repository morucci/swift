# Copyright (c) 2010-2011 OpenStack, LLC.
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

"""
This StaticWeb WSGI middleware will serve container data as a static web site
with index file and error file resolution and optional file listings. This mode
is normally only active for anonymous requests. If you want to use it with
authenticated requests, set the ``X-Web-Mode: true`` header on the request.

The ``staticweb`` filter should be added to the pipeline in your
``/etc/swift/proxy-server.conf`` file just after any auth middleware. Also, the
configuration section for the ``staticweb`` middleware itself needs to be
added. For example::

    [DEFAULT]
    ...

    [pipeline:main]
    pipeline = healthcheck cache swauth staticweb proxy-server

    ...

    [filter:staticweb]
    user = egg:swift#staticweb
    # Seconds to cache container x-container-meta-index,
    # x-container-meta-error, and x-container-listing-css header values.
    # cache_timeout = 300

Any publicly readable containers (for example, ``X-Container-Read: .r:*``, see
`acls`_ for more information on this) will be checked for
X-Container-Meta-Index and X-Container-Meta-Error header values::

    X-Container-Meta-Index  <index.name>
    X-Container-Meta-Error  <error.name.suffix>

If X-Container-Meta-Index is set, any <index.name> files will be served without
having to specify the <index.name> part. For instance, setting
``X-Container-Meta-Index: index.html`` will be able to serve the object
.../pseudo/path/index.html with just .../pseudo/path or .../pseudo/path/

If X-Container-Meta-Error is set, any errors (currently just 401 Unauthorized
and 404 Not Found) will instead serve the .../<status.code><error.name.suffix>
object. For instance, setting ``X-Container-Meta-Error: error.html`` will serve
.../404error.html for requests for paths not found.

For psuedo paths that have no <index.name>, this middleware will serve HTML
file listings by default. If you don't want to serve such listings, you can
turn this off via the `acls`_ X-Container-Read setting of ``.rnolisting``. For
example, instead of ``X-Container-Read: .r:*`` you would use
``X-Container-Read: .r:*,.rnolisting``

If listings are enabled, the listings can have a custom style sheet by setting
the X-Container-Meta-Listing-CSS header. For instance, setting
``X-Container-Meta-Listing-CSS: listing.css`` will make listings link to the
.../listing.css style sheet. If you "view source" in your browser on a listing
page, you will see the well defined document structure that can be styled.

Example usage of this middleware via ``st``:

    Make the container publicly readable::

        st post -r '.r:*' container

    You should be able to get objects and do direct container listings now,
    though they'll be in the REST API format.

    Set an index file directive::

        st post -m 'index:index.html' container

    You should be able to hit paths that have an index.html without needing to
    type the index.html part and listings will now be HTML.

    Turn off listings::

        st post -r '.r:*,.rnolisting' container

    Set an error file::

        st post -m 'error:error.html' container

    Now 401's should load 401error.html, 404's should load 404error.html, etc.

    Turn listings back on::

        st post -r '.r:*' container

    Enable a custom listing style sheet::

        st post -m 'listing-css:listing.css' container
"""


try:
    import simplejson as json
except ImportError:
    import json

import cgi
import urllib

from webob import Response, Request
from webob.exc import HTTPMovedPermanently, HTTPNotFound

from swift.common.utils import cache_from_env, human_readable, split_path, \
                               TRUE_VALUES


class StaticWeb(object):
    """
    The Static Web WSGI middleware filter; serves container data as a static
    web site. See `staticweb`_ for an overview.

    :param app: The next WSGI application/filter in the paste.deploy pipeline.
    :param conf: The filter configuration dict.
    """

    def __init__(self, app, conf):
        #: The next WSGI application/filter in the paste.deploy pipeline.
        self.app = app
        #: The filter configuration dict.
        self.conf = conf
        #: The seconds to cache the x-container-meta-index,
        #: x-container-meta-error, and x-container-listing-css headers for a
        #: container.
        self.cache_timeout = int(conf.get('cache_timeout', 300))
        # Results from the last call to self._start_response.
        self._response_status = None
        self._response_headers = None
        self._response_exc_info = None
        # Results from the last call to self._get_container_info.
        self._index = self._error = self._listing_css = None

    def _start_response(self, status, headers, exc_info=None):
        """
        Saves response info without sending it to the remote client.
        Uses the same semantics as the usual WSGI start_response.
        """
        self._response_status = status
        self._response_headers = headers
        self._response_exc_info = exc_info

    def _error_response(self, response, env, start_response):
        """
        Sends the error response to the remote client, possibly resolving a
        custom error response body based on x-container-meta-error.

        :param response: The error response we should default to sending.
        :param env: The original request WSGI environment.
        :param start_response: The WSGI start_response hook.
        """
        if not self._error:
            start_response(self._response_status, self._response_headers,
                           self._response_exc_info)
            return response
        save_response_status = self._response_status
        save_response_headers = self._response_headers
        save_response_exc_info = self._response_exc_info
        tmp_env = dict(env)
        self._strip_ifs(tmp_env)
        tmp_env['PATH_INFO'] = '/%s/%s/%s/%s%s' % (self.version, self.account,
            self.container, self._get_status_int(), self._error)
        tmp_env['REQUEST_METHOD'] = 'GET'
        resp = self.app(tmp_env, self._start_response)
        if self._get_status_int() // 100 == 2:
            start_response(save_response_status, self._response_headers,
                           self._response_exc_info)
            return resp
        start_response(save_response_status, save_response_headers,
                       save_response_exc_info)
        return response

    def _get_status_int(self):
        """
        Returns the HTTP status int from the last called self._start_response
        result.
        """
        return int(self._response_status.split(' ', 1)[0])

    def _strip_ifs(self, env):
        """ Strips any HTTP_IF_* keys from the env dict. """
        for key in [k for k in env.keys() if k.startswith('HTTP_IF_')]:
            del env[key]

    def _get_container_info(self, env, start_response):
        """
        Retrieves x-container-meta-index, x-container-meta-error, and
        x-container-meta-listing-css from memcache or from the cluster and
        stores the result in memcache and in self._index, self._error, and
        self._listing_css.

        :param env: The WSGI environment dict.
        :param start_response: The WSGI start_response hook.
        """
        self._index = self._error = self._listing_css = None
        memcache_client = cache_from_env(env)
        if memcache_client:
            memcache_key = '/staticweb/%s/%s/%s' % (self.version, self.account,
                                                    self.container)
            cached_data = memcache_client.get(memcache_key)
            if cached_data:
                self._index, self._error, self._listing_css = cached_data
                return
        tmp_env = {'REQUEST_METHOD': 'HEAD', 'HTTP_USER_AGENT': 'StaticWeb'}
        for name in ('swift.cache', 'HTTP_X_CF_TRANS_ID'):
            if name in env:
                tmp_env[name] = env[name]
        req = Request.blank('/%s/%s/%s' % (self.version, self.account,
            self.container), environ=tmp_env)
        resp = req.get_response(self.app)
        if resp.status_int // 100 == 2:
            self._index = \
                resp.headers.get('x-container-meta-index', '').strip()
            self._listing_css = \
                resp.headers.get('x-container-meta-listing-css', '').strip()
            self._error = \
                resp.headers.get('x-container-meta-error', '').strip()
            if memcache_client:
                memcache_client.set(memcache_key,
                    (self._index, self._error, self._listing_css),
                    timeout=self.cache_timeout)

    def _listing(self, env, start_response, prefix=None):
        """
        Sends an HTML object listing to the remote client.

        :param env: The original WSGI environment dict.
        :param start_response: The original WSGI start_response hook.
        :param prefix: Any prefix desired for the container listing.
        """
        tmp_env = dict(env)
        self._strip_ifs(tmp_env)
        tmp_env['REQUEST_METHOD'] = 'GET'
        tmp_env['PATH_INFO'] = \
            '/%s/%s/%s' % (self.version, self.account, self.container)
        tmp_env['QUERY_STRING'] = 'delimiter=/&format=json'
        if prefix:
            tmp_env['QUERY_STRING'] += '&prefix=%s' % urllib.quote(prefix)
        resp = self.app(tmp_env, self._start_response)
        if self._get_status_int() // 100 != 2:
            return self._error_response(resp, env, start_response)
        listing = json.loads(''.join(resp))
        if not listing:
            resp = HTTPNotFound()(env, self._start_response)
            return self._error_response(resp, env, start_response)
        headers = {'Content-Type': 'text/html'}
        body = '<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 4.01 ' \
                'Transitional//EN" "http://www.w3.org/TR/html4/loose.dtd">\n' \
               '<html>\n' \
               ' <head>\n' \
               '  <title>Listing of %s</title>\n' % \
               cgi.escape(env['PATH_INFO'])
        if self._listing_css:
            body += '  <link rel="stylesheet" type="text/css" ' \
                        'href="/%s/%s/%s/%s" />\n' % \
                    (self.version, self.account, self.container,
                     urllib.quote(self._listing_css))
        else:
            body += '  <style type="text/css">\n' \
                    '   h1 {font-size: 1em; font-weight: bold;}\n' \
                    '   th {text-align: left; padding: 0px 1em 0px 1em;}\n' \
                    '   td {padding: 0px 1em 0px 1em;}\n' \
                    '   a {text-decoration: none;}\n' \
                    '  </style>\n'
        body += ' </head>\n' \
                ' <body>\n' \
                '  <h1 id="title">Listing of %s</h1>\n' \
                '  <table id="listing">\n' \
                '   <tr id="heading">\n' \
                '    <th class="colname">Name</th>\n' \
                '    <th class="colsize">Size</th>\n' \
                '    <th class="coldate">Date</th>\n' \
                '   </tr>\n' % \
                cgi.escape(env['PATH_INFO'])
        if prefix:
            body += '   <tr id="parent" class="item">\n' \
                    '    <td class="colname"><a href="../">../</a></td>\n' \
                    '    <td class="colsize">&nbsp;</td>\n' \
                    '    <td class="coldate">&nbsp;</td>\n' \
                    '   </tr>\n'
        for item in listing:
            if 'subdir' in item:
                subdir = item['subdir']
                if prefix:
                    subdir = subdir[len(prefix):]
                body += '   <tr class="item subdir">\n' \
                        '    <td class="colname"><a href="%s">%s</a></td>\n' \
                        '    <td class="colsize">&nbsp;</td>\n' \
                        '    <td class="coldate">&nbsp;</td>\n' \
                        '   </tr>\n' % \
                        (urllib.quote(subdir), cgi.escape(subdir))
        for item in listing:
            if 'name' in item:
                name = item['name']
                if prefix:
                    name = name[len(prefix):]
                body += '   <tr class="item %s">\n' \
                        '    <td class="colname"><a href="%s">%s</a></td>\n' \
                        '    <td class="colsize">%s</td>\n' \
                        '    <td class="coldate">%s</td>\n' \
                        '   </tr>\n' % \
                        (' '.join('type-' + cgi.escape(t.lower(), quote=True)
                                  for t in item['content_type'].split('/')),
                         urllib.quote(name), cgi.escape(name),
                         human_readable(item['bytes']),
                         cgi.escape(item['last_modified']).split('.')[0].
                            replace('T', ' '))
        body += '  </table>\n' \
                ' </body>\n' \
                '</html>\n'
        return Response(headers=headers, body=body)(env, start_response)

    def _handle_container(self, env, start_response):
        """
        Handles a possible static web request for a container.

        :param env: The original WSGI environment dict.
        :param start_response: The original WSGI start_response hook.
        """
        self._get_container_info(env, start_response)
        if not self._index:
            return self.app(env, start_response)
        if env['PATH_INFO'][-1] != '/':
            return HTTPMovedPermanently(
                location=(env['PATH_INFO'] + '/'))(env, start_response)
        tmp_env = dict(env)
        tmp_env['PATH_INFO'] += self._index
        resp = self.app(tmp_env, self._start_response)
        status_int = self._get_status_int()
        if status_int == 404:
            return self._listing(env, start_response)
        elif self._get_status_int() // 100 not in (2, 3):
            return self._error_response(resp, env, start_response)
        start_response(self._response_status, self._response_headers,
                       self._response_exc_info)
        return resp

    def _handle_object(self, env, start_response):
        """
        Handles a possible static web request for an object. This object could
        resolve into an index or listing request.

        :param env: The original WSGI environment dict.
        :param start_response: The original WSGI start_response hook.
        """
        tmp_env = dict(env)
        resp = self.app(tmp_env, self._start_response)
        status_int = self._get_status_int()
        if status_int // 100 in (2, 3):
            start_response(self._response_status, self._response_headers,
                           self._response_exc_info)
            return resp
        if status_int != 404:
            return self._error_response(resp, env, start_response)
        self._get_container_info(env, start_response)
        if not self._index:
            return self.app(env, start_response)
        tmp_env = dict(env)
        if tmp_env['PATH_INFO'][-1] != '/':
            tmp_env['PATH_INFO'] += '/'
        tmp_env['PATH_INFO'] += self._index
        resp = self.app(tmp_env, self._start_response)
        status_int = self._get_status_int()
        if status_int // 100 in (2, 3):
            if env['PATH_INFO'][-1] != '/':
                return HTTPMovedPermanently(
                    location=env['PATH_INFO'] + '/')(env, start_response)
            start_response(self._response_status, self._response_headers,
                           self._response_exc_info)
            return resp
        elif status_int == 404:
            if env['PATH_INFO'][-1] != '/':
                tmp_env = dict(env)
                self._strip_ifs(tmp_env)
                tmp_env['REQUEST_METHOD'] = 'GET'
                tmp_env['PATH_INFO'] = '/%s/%s/%s' % (self.version,
                    self.account, self.container)
                tmp_env['QUERY_STRING'] = 'limit=1&format=json&delimiter' \
                    '=/&limit=1&prefix=%s' % urllib.quote(self.obj + '/')
                resp = self.app(tmp_env, self._start_response)
                if self._get_status_int() // 100 != 2 or \
                        not json.loads(''.join(resp)):
                    resp = HTTPNotFound()(env, self._start_response)
                    return self._error_response(resp, env, start_response)
                return HTTPMovedPermanently(location=env['PATH_INFO'] +
                    '/')(env, start_response)
            return self._listing(env, start_response, self.obj)

    def __call__(self, env, start_response):
        """
        Main hook into the WSGI paste.deploy filter/app pipeline.

        :param env: The WSGI environment dict.
        :param start_response: The WSGI start_response hook.
        """
        try:
            (self.version, self.account, self.container, self.obj) = \
                split_path(env['PATH_INFO'], 2, 4, True)
        except ValueError:
            return self.app(env, start_response)
        memcache_client = cache_from_env(env)
        if memcache_client:
            if env['REQUEST_METHOD'] in ('PUT', 'POST'):
                if not self.obj and self.container:
                    memcache_key = '/staticweb/%s/%s/%s' % \
                        (self.version, self.account, self.container)
                    memcache_client.delete(memcache_key)
                return self.app(env, start_response)
        if env['REQUEST_METHOD'] not in ('HEAD', 'GET') or \
                (env.get('REMOTE_USER') and
                 env.get('HTTP_X_WEB_MODE', '') not in TRUE_VALUES):
            return self.app(env, start_response)
        if self.obj:
            return self._handle_object(env, start_response)
        elif self.container:
            return self._handle_container(env, start_response)
        return self.app(env, start_response)


def filter_factory(global_conf, **local_conf):
    """ Returns a Static Web WSGI filter for use with paste.deploy. """
    conf = global_conf.copy()
    conf.update(local_conf)

    def staticweb_filter(app):
        return StaticWeb(app, conf)
    return staticweb_filter
