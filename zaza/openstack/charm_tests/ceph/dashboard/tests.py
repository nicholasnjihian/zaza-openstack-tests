# Copyright 2021 Canonical Ltd.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Encapsulating `ceph-dashboard` testing."""

import collections
import json
import logging
import requests
import tempfile
import tenacity
import uuid

import zaza
import zaza.openstack.charm_tests.test_utils as test_utils
import zaza.openstack.utilities.openstack as openstack_utils


SAML_IDP_METADATA = '''
<ds:KeyInfo xmlns:ds="http://www.w3.org/2000/09/xmldsig#">
    <ds:X509Data>
        <ds:X509Certificate>
            {}
        </ds:X509Certificate>
    </ds:X509Data>
</ds:KeyInfo>
'''


class CephDashboardTest(test_utils.BaseCharmTest):
    """Class for `ceph-dashboard` tests."""

    REMOTE_CERT_FILE = ('/usr/local/share/ca-certificates/'
                        'vault_ca_cert_dashboard.crt')

    @classmethod
    def setUpClass(cls):
        """Run class setup for running ceph dashboard tests."""
        super().setUpClass()
        cls.application_name = 'ceph-dashboard'
        cls.local_ca_cert = openstack_utils.get_remote_ca_cert_file(
            cls.application_name)

    @tenacity.retry(wait=tenacity.wait_exponential(multiplier=1,
                                                   min=5, max=10),
                    retry=tenacity.retry_if_exception_type(
                        requests.exceptions.ConnectionError),
                    reraise=True)
    def _run_request_get(self, url, verify, allow_redirects):
        """Run a GET request against `url` with tenacity retries.

        :param url: url to access
        :type url: str
        :param verify: Path to a CA_BUNDLE file or directory with certificates
                       of trusted CAs or False to ignore verifying the SSL
                       certificate.
        :type verify: Union[str, bool]
        :param allow_redirects: Set to True if redirect following is allowed.
        :type allow_redirects: bool
        :returns: Request response
        :rtype: requests.models.Response
        """
        return requests.get(
            url,
            verify=verify,
            allow_redirects=allow_redirects)

    @tenacity.retry(wait=tenacity.wait_exponential(multiplier=1,
                                                   min=5, max=10),
                    retry=tenacity.retry_if_exception_type(
                        requests.exceptions.ConnectionError),
                    reraise=True)
    def _run_request_post(self, url, verify, data, headers):
        """Run a POST request against `url` with tenacity retries.

        :param url: url to access
        :type url: str
        :param verify: Path to a CA_BUNDLE file or directory with certificates
                       of trusted CAs or False to ignore verifying the SSL
                       certificate.
        :type verify: Union[str, bool]
        :param data: Data to post to url
        :type data: str
        :param headers: Headers to set when posting
        :type headers: dict
        :returns: Request response
        :rtype: requests.models.Response
        """
        return requests.post(
            url,
            data=data,
            headers=headers,
            verify=verify)

    def get_master_dashboard_url(self):
        """Get the url of the dashboard servicing requests.

        Only one unit serves requests at any one time, the other units
        redirect to that unit.

        :returns: URL of dashboard on unit
        :rtype: Union[str, None]
        """
        units = zaza.model.get_units(self.application_name)
        for unit in units:
            r = self._run_request_get(
                'https://{}:8443'.format(
                    zaza.model.get_unit_public_address(unit)),
                verify=self.local_ca_cert,
                allow_redirects=False)
            if r.status_code == requests.codes.ok:
                return 'https://{}:8443'.format(
                    zaza.model.get_unit_public_address(unit))

    def test_dashboard_units(self):
        """Check dashboard units are configured correctly."""
        verify = self.local_ca_cert
        units = zaza.model.get_units(self.application_name)
        rcs = collections.defaultdict(list)
        for unit in units:
            r = self._run_request_get(
                'https://{}:8443'.format(
                    zaza.model.get_unit_public_address(unit)),
                verify=verify,
                allow_redirects=False)
            rcs[r.status_code].append(zaza.model.get_unit_public_address(unit))
        self.assertEqual(len(rcs[requests.codes.ok]), 1)
        self.assertEqual(len(rcs[requests.codes.see_other]), len(units) - 1)

    def create_user(self, username, role='administrator'):
        """Create a dashboard user.

        :param username: Username to create.
        :type username: str
        :param role: Role to grant to user.
        :type role: str
        :returns: Results from action.
        :rtype: juju.action.Action
        """
        action = zaza.model.run_action_on_leader(
            'ceph-dashboard',
            'add-user',
            action_params={
                'username': username,
                'role': role})
        return action

    def get_random_username(self):
        """Generate a username to use in tests.

        :returns: Username
        :rtype: str
        """
        return "zazauser-{}".format(uuid.uuid1())

    def test_create_user(self):
        """Test create user action."""
        test_user = self.get_random_username()
        action = self.create_user(test_user)
        self.assertEqual(action.status, "completed")
        self.assertTrue(action.data['results']['password'])
        action = self.create_user(test_user)
        # Action should fail as the user already exists
        self.assertEqual(action.status, "failed")

    def access_dashboard(self, dashboard_url):
        """Test logging via a dashboard url.

        :param dashboard_url: Base url to use to login to
        :type dashboard_url: str
        """
        user = self.get_random_username()
        action = self.create_user(username=user)
        self.assertEqual(action.status, "completed")
        password = action.data['results']['password']
        path = "api/auth"
        headers = {
            'Content-type': 'application/json',
            'Accept': 'application/vnd.ceph.api.v1.0'}
        payload = {"username": user, "password": password}
        verify = self.local_ca_cert
        r = self._run_request_post(
            "{}/{}".format(dashboard_url, path),
            verify=verify,
            data=json.dumps(payload),
            headers=headers)
        self.assertEqual(r.status_code, requests.codes.created)

    def test_access_dashboard(self):
        """Test logging in to the dashboard."""
        self.access_dashboard(self.get_master_dashboard_url())

    def test_ceph_keys(self):
        """Check that ceph services are properly registered."""
        status = zaza.model.get_status()
        applications = status.applications.keys()
        dashboard_keys = []
        ceph_keys = []
        if 'ceph-radosgw' in applications:
            dashboard_keys.extend(['RGW_API_ACCESS_KEY', 'RGW_API_SECRET_KEY'])
        if 'grafana' in applications:
            dashboard_keys.append('GRAFANA_API_URL')
        if 'prometheus' in applications:
            dashboard_keys.append('PROMETHEUS_API_HOST')
        ceph_keys.extend(
            ['config/mgr/mgr/dashboard/{}'.format(k) for k in dashboard_keys])
        if 'ceph-iscsi' in applications:
            ceph_keys.append('mgr/dashboard/_iscsi_config')
        for key in ceph_keys:
            logging.info("Checking key {} exists".format(key))
            check_out = zaza.model.run_on_leader(
                'ceph-dashboard',
                'ceph config-key exists {}'.format(key))
            self.assertEqual(check_out['Code'], '0')

    def test_saml(self):
        """Check that the dashboard is accessible with SAML enabled."""
        get_os_release = openstack_utils.get_os_release
        if (get_os_release(application='ceph-mon') <
                get_os_release('focal_yoga')):
            return

        url = self.get_master_dashboard_url()
        with tempfile.NamedTemporaryFile(mode='w') as tmp, \
                open(self.local_ca_cert) as cert:
            tmp.write(SAML_IDP_METADATA.format(cert.read()))
            tmp.flush()
            zaza.model.set_application_config(
                'ceph-dashboard',
                {
                    'saml-base-url': url,
                    'saml-idp-metadata': 'file://{}'.format(tmp.name),
                }
            )

            # Login must be redirected.
            resp = requests.get(url + '/auth/saml2/login')
            self.assertTrue(resp.is_redirect)

            # Check that metadata is present.
            resp = requests.get(url + '/auth/saml2/metadata')
            self.assertEqual(resp.status_code, requests.code.ok)
