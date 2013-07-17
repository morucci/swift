# Copyright (c) 2013 OpenStack Foundation.
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
``account_quotas`` is a middleware which blocks write requests (PUT, POST) if a
given account quota (in bytes) is exceeded while DELETE requests are still
allowed.

``account_quotas`` uses the ``x-account-meta-quota-bytes`` metadata entry to
store the quota. Write requests to this metadata entry are only permitted for
resellers. There is no quota limit if ``x-account-meta-quota-bytes`` is not
set.

The ``account_quotas`` middleware should be added to the pipeline in your
``/etc/swift/proxy-server.conf`` file just after any auth middleware.
For example::

    [pipeline:main]
    pipeline = catch_errors cache tempauth account_quotas proxy-server

    [filter:account_quotas]
    use = egg:swift#account_quotas

To set the quota on an account::

    swift -A http://127.0.0.1:8080/auth/v1.0 -U account:reseller -K secret \
post -m quota-bytes:10000

Remove the quota::

    swift -A http://127.0.0.1:8080/auth/v1.0 -U account:reseller -K secret \
post -m quota-bytes:

"""


from swift.common.swob import HTTPForbidden, HTTPRequestEntityTooLarge, \
    HTTPBadRequest, wsgify
from swift.common.wsgi import make_pre_authed_request
from swift.proxy.controllers.base import get_account_info


def get_object_size(app, req, path):
    req = make_pre_authed_request(req.environ,
                                  method='HEAD',
                                  path=path,
                                  swift_source='aq')
    resp = req.get_response(app)
    if resp.is_success:
        return int(resp.headers.get('content-length'))


class AccountQuotaMiddleware(object):
    """ Account quota middleware

    See above for a full description.

    """
    def __init__(self, app, *args, **kwargs):
        self.app = app

    @wsgify
    def __call__(self, request):

        if request.method not in ("POST", "PUT"):
            return self.app

        try:
            ver, acc, cont, obj = request.split_path(2, 4, rest_with_last=True)
        except ValueError:
            return self.app

        new_quota = request.headers.get('X-Account-Meta-Quota-Bytes')

        if request.environ.get('reseller_request') is True:
            if new_quota and not new_quota.isdigit():
                return HTTPBadRequest()
            return self.app

        # deny quota set for non-reseller
        if new_quota is not None:
            return HTTPForbidden()
        
        copy_from = request.headers.get('X-Copy-From')
        content_length = (request.content_length or 0)

        if obj and copy_from:
            path = '/' + ver + '/' + acc + '/' + copy_from.lstrip('/')
            content_length = (get_object_size(self.app, request, path) or 0)

        account_info = get_account_info(request.environ, self.app)
        if not account_info or not account_info['bytes']:
            return self.app

        new_size = int(account_info['bytes']) + content_length
        quota = int(account_info['meta'].get('quota-bytes', -1))

        if 0 <= quota < new_size:
            return HTTPRequestEntityTooLarge()

        return self.app


def filter_factory(global_conf, **local_conf):
    """Returns a WSGI filter app for use with paste.deploy."""
    def account_quota_filter(app):
        return AccountQuotaMiddleware(app)
    return account_quota_filter
