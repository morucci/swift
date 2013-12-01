# Copyright (c) 2010-2012 OpenStack Foundation
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

import mock
import unittest

from swift.common.swob import Request
from swift.proxy import server as proxy_server
from swift.proxy.controllers.base import headers_to_account_info
from swift.common.constraints import MAX_ACCOUNT_NAME_LENGTH as MAX_ANAME_LEN
from test.unit import fake_http_connect, FakeRing, FakeMemcache


class TestAccountController(unittest.TestCase):
    def setUp(self):
        self.app = proxy_server.Application(None, FakeMemcache(),
                                            account_ring=FakeRing(),
                                            container_ring=FakeRing(),
                                            object_ring=FakeRing())

    def test_account_info_in_response_env(self):
        controller = proxy_server.AccountController(self.app, 'AUTH_bob')
        with mock.patch('swift.proxy.controllers.base.http_connect',
                        fake_http_connect(200, body='')):
            req = Request.blank('/AUTH_bob', {'PATH_INFO': '/AUTH_bob'})
            resp = controller.HEAD(req)
        self.assertEqual(2, resp.status_int // 100)
        self.assertTrue('swift.account/AUTH_bob' in resp.environ)
        self.assertEqual(headers_to_account_info(resp.headers),
                         resp.environ['swift.account/AUTH_bob'])

    def test_swift_owner(self):
        owner_headers = {
            'x-account-meta-temp-url-key': 'value',
            'x-account-meta-temp-url-key-2': 'value'}
        controller = proxy_server.AccountController(self.app, 'a')

        req = Request.blank('/a')
        with mock.patch('swift.proxy.controllers.base.http_connect',
                        fake_http_connect(200, headers=owner_headers)):
            resp = controller.HEAD(req)
        self.assertEquals(2, resp.status_int // 100)
        for key in owner_headers:
            self.assertTrue(key not in resp.headers)

        # clean previously cached about account
        self.app.memcache.delete('account/a')

        req = Request.blank('/a', environ={'swift_owner': True})
        with mock.patch('swift.proxy.controllers.base.http_connect',
                        fake_http_connect(200, headers=owner_headers)):
            resp = controller.HEAD(req)
        self.assertEquals(2, resp.status_int // 100)
        for key in owner_headers:
            self.assertTrue(key in resp.headers)

    def test_get_deleted_account(self):
        resp_headers = {
            'x-account-status': 'deleted',
        }
        controller = proxy_server.AccountController(self.app, 'a')

        req = Request.blank('/a')
        with mock.patch('swift.proxy.controllers.base.http_connect',
                        fake_http_connect(404, headers=resp_headers)):
            resp = controller.HEAD(req)
        self.assertEquals(410, resp.status_int)

    def test_long_acct_names(self):
        long_acct_name = '%sLongAccountName' % ('Very' * (MAX_ANAME_LEN // 4))
        controller = proxy_server.AccountController(self.app, long_acct_name)

        req = Request.blank('/%s' % long_acct_name)
        with mock.patch('swift.proxy.controllers.base.http_connect',
                        fake_http_connect(200)):
            resp = controller.HEAD(req)
        self.assertEquals(400, resp.status_int)

        with mock.patch('swift.proxy.controllers.base.http_connect',
                        fake_http_connect(200)):
            resp = controller.GET(req)
        self.assertEquals(400, resp.status_int)

        with mock.patch('swift.proxy.controllers.base.http_connect',
                        fake_http_connect(200)):
            resp = controller.POST(req)
        self.assertEquals(400, resp.status_int)


if __name__ == '__main__':
    unittest.main()
