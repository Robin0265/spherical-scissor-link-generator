"""Tests for ssm.mcmaster - the McMaster-Carr part lookup.

Runs on plain Python (no Fusion, no network):

    python -m unittest discover -s tests -v

The online path is exercised against a local stand-in for the McMaster
Product Information API (https://www.mcmaster.com/help/api/): a real HTTPS
server that, like McMaster's, refuses any connection without a client
certificate, and speaks the documented request/response shapes (login ->
AuthToken; PUT /v1/products -> PartNumber/Specifications/Links; GET
/v1/cad/...; errors as ErrorCode/ErrorMessage/ErrorDescription, with 403
EXPIRED_AUTHORIZATION_TOKEN when a token runs out). The certificates are
generated per run with the openssl CLI; the TLS tests skip if it is absent.

mcmaster.com itself is never contacted: the stand-in is the only server any
test talks to, and the offline tests assert that no request is made at all.
"""

import json
import os
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, 'SphericalScissorGenerator'))

from ssm import mcmaster  # noqa: E402  (no adsk import - runs anywhere)


# ---------------------------------------------------------------------------
# Fixtures

def make_step(od_mm, id_mm, length_mm):
    """A minimal STEP text with the entities dims_from_step reads: an outer
    and an inner cylinder on a shared Z axis, and points spanning the
    length. Synthetic on purpose - no McMaster CAD is committed."""
    lines = [
        'ISO-10303-21;', 'HEADER;', 'ENDSEC;', 'DATA;',
        "#1=(LENGTH_UNIT()NAMED_UNIT(*)SI_UNIT(.MILLI.,.METRE.));",
        "#10=CARTESIAN_POINT('',(0.,0.,0.));",
        "#11=DIRECTION('',(0.,0.,1.));",
        "#12=DIRECTION('',(1.,0.,0.));",
        "#13=AXIS2_PLACEMENT_3D('',#10,#11,#12);",
        "#20=CYLINDRICAL_SURFACE('',#13,%s);" % repr(od_mm / 2.0),
        "#21=CYLINDRICAL_SURFACE('',#13,%s);" % repr(id_mm / 2.0),
        "#30=CARTESIAN_POINT('',(%s,0.,0.));" % repr(od_mm / 2.0),
        "#31=CARTESIAN_POINT('',(%s,0.,%s));" % (repr(od_mm / 2.0), repr(length_mm)),
        'ENDSEC;', 'END-ISO-10303-21;',
    ]
    return '\n'.join(lines).encode('ascii')


BEARING_PN, BUSHING_PN, NO_CAD_PN = '7806K58', '6658K727', '1234K56'
STEP_LINK = {
    BEARING_PN: '/v1/cad/library/20261101/7806K58_Ball%20Thrust%20Bearing.STEP',
    BUSHING_PN: '/v1/cad/library/20261101/6658K727_Sleeve%20Bearing.STEP',
}
CAD = {
    STEP_LINK[BEARING_PN]: make_step(12.0, 6.0, 4.5),
    STEP_LINK[BUSHING_PN]: make_step(10.0, 6.0, 6.0),
}


def product(pn, family, specs, step=True):
    links = [{'Key': 'Price', 'Value': '/v1/products/%s/price' % pn},
             {'Key': '2-D DWG', 'Value': '/v1/cad/library/x/%s.DWG' % pn}]
    if step:
        links.append({'Key': '3-D STEP', 'Value': STEP_LINK[pn]})
    return {'PartNumber': pn, 'ProductStatus': 'Active',
            'FamilyDescription': family, 'DetailDescription': '',
            'Specifications': [{'Attribute': k, 'Values': [v]} for k, v in specs],
            'Links': links}


PRODUCTS = {
    BEARING_PN: product(BEARING_PN, 'Ball Thrust Bearing', [
        ('For Shaft Diameter', '6 mm'), ('ID', '6 mm'), ('OD', '12 mm'),
        ('Height', '4.5 mm'), ('Material', 'Steel')]),
    BUSHING_PN: product(BUSHING_PN, 'Sleeve Bearing', [
        ('For Shaft Diameter', '6 mm'), ('ID', '6 mm'), ('OD', '10 mm'),
        ('Length', '6 mm')]),
    NO_CAD_PN: product(NO_CAD_PN, 'Thrust Bearing', [
        ('OD', '1/2"'), ('ID', '1/4"'), ('Thickness', '0.125"')], step=False),
}
USER, PASSWORD = 'robin@example.com', 'account-password'


# ---------------------------------------------------------------------------
# The stand-in API

class FakeApi:
    """State shared by the handler threads; reset per test."""

    def __init__(self):
        self.requests = []      # (method, path)
        self.tokens = {}        # token -> expired?
        self.logins = 0

    def expire_all_tokens(self):
        for token in self.tokens:
            self.tokens[token] = True


class Handler(BaseHTTPRequestHandler):
    api = None  # set per server

    def log_message(self, *args):
        pass

    def _send(self, code, payload, content_type='application/json'):
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(code)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, code, message, description):
        self._send(code, {'ErrorCode': code, 'ErrorMessage': message,
                          'ErrorDescription': description})

    def _body(self):
        length = int(self.headers.get('Content-Length') or 0)
        return json.loads(self.rfile.read(length) or b'{}')

    def _authorised(self):
        header = self.headers.get('Authorization') or ''
        token = header[len('Bearer '):] if header.startswith('Bearer ') else ''
        if token not in self.api.tokens:
            self._error(403, 'INVALID_AUTHORIZATION_TOKEN', 'Log in first.')
            return False
        if self.api.tokens[token]:
            self._error(403, 'EXPIRED_AUTHORIZATION_TOKEN',
                        'The authorization token has expired.')
            return False
        return True

    def do_POST(self):
        self.api.requests.append(('POST', self.path))
        if self.path != '/v1/login':
            return self._error(404, 'NOT_FOUND', self.path)
        body = self._body()
        if body.get('UserName') != USER or body.get('Password') != PASSWORD:
            return self._error(401, 'UNAUTHORIZED',
                               'The user name or password is incorrect.')
        self.api.logins += 1
        token = 'token-%d' % self.api.logins
        self.api.tokens[token] = False
        self._send(200, {'AuthToken': token,
                         'ExpirationTS': '2026-11-02T19:00:00-05:00'})

    def do_PUT(self):
        self.api.requests.append(('PUT', self.path))
        if self.path != '/v1/products':
            return self._error(404, 'NOT_FOUND', self.path)
        if not self._authorised():
            return
        url = self._body().get('URL') or ''
        pn = url.rstrip('/').rsplit('/', 1)[-1].upper()
        if pn not in PRODUCTS:
            return self._error(400, 'BAD_REQUEST',
                               'Part number %s is invalid.' % pn)
        self._send(201, PRODUCTS[pn])

    def do_GET(self):
        self.api.requests.append(('GET', self.path))
        if not self._authorised():
            return
        if self.path not in CAD:
            return self._error(400, 'BAD_REQUEST', 'Invalid file path.')
        self._send(200, CAD[self.path], 'application/octet-stream')


def _openssl(*args, cwd):
    # A config of our own: Windows builds of openssl (Git, Strawberry Perl)
    # often ship without a usable default openssl.cnf, and `req` needs one.
    env = dict(os.environ, OPENSSL_CONF=os.path.join(cwd, 'openssl.cnf'))
    result = subprocess.run(('openssl',) + args, cwd=cwd, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode != 0:
        raise unittest.SkipTest('openssl could not make test certificates: %s'
                                % result.stderr.decode('utf-8', 'replace').strip()[-300:])


def make_certificates(folder):
    """CA, server cert for localhost, and a client cert in one PEM (the
    shape the notes' `openssl pkcs12 ... -nodes` conversion produces)."""
    # Complete X.509 extensions: Python 3.13+ (Fusion bundles 3.14) verifies
    # with VERIFY_X509_STRICT, which rejects a CA without basicConstraints /
    # keyUsage and leaf certificates without an authority key identifier.
    with open(os.path.join(folder, 'openssl.cnf'), 'w') as fh:
        fh.write('[req]\ndistinguished_name = dn\nprompt = no\n'
                 'x509_extensions = v3_ca\n[dn]\nCN = test\n'
                 '[v3_ca]\nbasicConstraints = critical, CA:TRUE\n'
                 'keyUsage = critical, keyCertSign, cRLSign\n'
                 'subjectKeyIdentifier = hash\n'
                 'authorityKeyIdentifier = keyid:always\n')
    leaf = ('basicConstraints = critical, CA:FALSE\n'
            'keyUsage = critical, digitalSignature, keyEncipherment\n'
            'subjectKeyIdentifier = hash\n'
            'authorityKeyIdentifier = keyid, issuer\n')
    with open(os.path.join(folder, 'san.cnf'), 'w') as fh:
        fh.write(leaf + 'extendedKeyUsage = serverAuth\n'
                 'subjectAltName = DNS:localhost, IP:127.0.0.1\n')
    with open(os.path.join(folder, 'client.cnf'), 'w') as fh:
        fh.write(leaf + 'extendedKeyUsage = clientAuth\n')
    _openssl('req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '2',
             '-keyout', 'ca.key', '-out', 'ca.pem', '-subj', '/CN=Test CA', cwd=folder)
    for name, extra in (('server', ('-extfile', 'san.cnf')),
                        ('client', ('-extfile', 'client.cnf'))):
        _openssl('req', '-newkey', 'rsa:2048', '-nodes', '-keyout', name + '.key',
                 '-out', name + '.csr', '-subj', '/CN=' + name, cwd=folder)
        _openssl('x509', '-req', '-in', name + '.csr', '-CA', 'ca.pem',
                 '-CAkey', 'ca.key', '-CAcreateserial', '-days', '2',
                 '-out', name + '.pem', *extra, cwd=folder)
    combined = os.path.join(folder, 'client-combined.pem')
    with open(combined, 'wb') as out:
        for part in ('client.pem', 'client.key'):
            with open(os.path.join(folder, part), 'rb') as fh:
                out.write(fh.read())
    return combined


# ---------------------------------------------------------------------------
# Base: an isolated repo root, cache, home and Downloads per test

class IsolatedCase(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='ssm-mcm-')
        self.downloads = os.path.join(self.tmp, 'Downloads')
        os.makedirs(self.downloads)
        self._saved = {
            '_repo_root': mcmaster._repo_root,
            'downloads_dirs': mcmaster.downloads_dirs,
            'API_BASE': mcmaster.API_BASE,
            '_session': mcmaster._session,
        }
        self._env = {k: os.environ.get(k)
                     for k in ('HOME', 'USERPROFILE', 'SSL_CERT_FILE')}
        mcmaster._repo_root = lambda: self.tmp
        mcmaster.downloads_dirs = lambda: [self.downloads]
        mcmaster._session = None
        home = os.path.join(self.tmp, 'home')
        os.makedirs(home)
        os.environ['HOME'] = os.environ['USERPROFILE'] = home

    def tearDown(self):
        for name, value in self._saved.items():
            setattr(mcmaster, name, value)
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(self.tmp, ignore_errors=True)

    def cache_files(self):
        return sorted(os.listdir(mcmaster.cache_dir()))


class NoNetwork:
    """Fails the test if anything opens a URL inside the block."""

    def __enter__(self):
        self.calls = []
        self._open = urllib.request.OpenerDirector.open
        calls = self.calls

        def refuse(opener, request, *args, **kwargs):
            calls.append(getattr(request, 'full_url', request))
            raise AssertionError('network access attempted: %r' % (request,))

        urllib.request.OpenerDirector.open = refuse
        return self

    def __exit__(self, *exc):
        urllib.request.OpenerDirector.open = self._open
        return False


# ---------------------------------------------------------------------------
# Offline behaviour

class PartNumberTests(unittest.TestCase):

    def test_complete_numbers_accepted(self):
        for pn in ('7806K58', '6658K727', '91251A540', ' 7806k58 '):
            self.assertTrue(mcmaster.is_part_number(pn), pn)

    def test_partial_numbers_rejected(self):
        for pn in ('', '7', '780', '7806', '7806K', 'K58', '7806-K58'):
            self.assertFalse(mcmaster.is_part_number(pn), pn)


class ParseLengthTests(unittest.TestCase):

    def test_catalogue_formats(self):
        cases = {'1/4"': 6.35, '1-3/4"': 44.45, '0.257"': 6.5278,
                 '5 mm': 5.0, '5mm': 5.0, '13/16"': 20.6375, '1.2 cm': 12.0,
                 '1/2': 12.7}
        for text, mm in cases.items():
            self.assertAlmostEqual(mcmaster.parse_length_mm(text), mm, places=3,
                                   msg=text)

    def test_non_dimensions_are_none(self):
        for text in ('Steel', '', None, 'No. 10', '18-8'):
            self.assertIsNone(mcmaster.parse_length_mm(text), text)


class SizingRuleTests(unittest.TestCase):
    """hardware_autofill / hardware_problem: what fetched parts set and
    which geometry they forbid. Sizes are mm."""

    BEARING = {'od_mm': 12.0, 'id_mm': 6.0, 'thickness_mm': 4.5, 'description': ''}
    BUSHING = {'od_mm': 10.0, 'id_mm': 6.0, 'length_mm': 6.0, 'description': ''}

    def sizes(self, **changes):
        sizes = {'bar_width': 12.0, 'bar_thickness': 6.0, 'l_offset': 1.5,
                 'bearing_OD': 10.0, 'bearing_thickness': 4.5}
        sizes.update(changes)
        return sizes

    def test_autofill_sets_bore_and_gap_but_leaves_l_offset(self):
        expressions, warnings = mcmaster.hardware_autofill(
            self.BEARING, self.BUSHING, 6.0, 12.0)
        self.assertEqual(expressions, {'bearing_OD': '10 mm',
                                       'bearing_thickness': '4.5 mm'})
        self.assertEqual(warnings, [])

    def test_autofill_never_raises_for_a_long_bushing(self):
        # The length limit is a validation problem, not an autofill error,
        # so the bearing's values still apply.
        expressions, _ = mcmaster.hardware_autofill(
            self.BEARING, self.BUSHING, 3.0, 12.0)
        self.assertIn('bearing_thickness', expressions)

    def test_bushing_shorter_or_equal_to_bar_is_fine(self):
        for bar in (6.0, 6.01, 8.0):
            self.assertIsNone(mcmaster.hardware_problem(
                self.BEARING, self.BUSHING, self.sizes(bar_thickness=bar)), bar)

    def test_bushing_longer_than_bar_is_refused(self):
        problem = mcmaster.hardware_problem(
            self.BEARING, self.BUSHING, self.sizes(bar_thickness=3.0))
        self.assertIn('exceeds bar_thickness', problem)

    def test_l_offset_is_free(self):
        for l_offset in (0.0, 1.5, 4.0):
            self.assertIsNone(mcmaster.hardware_problem(
                None, self.BUSHING, self.sizes(l_offset=l_offset)), l_offset)

    def test_bushing_wider_than_boss_is_refused(self):
        problem = mcmaster.hardware_problem(
            None, self.BUSHING, self.sizes(bar_width=10.0))
        self.assertIn('no wall', problem)

    def test_bearing_wider_than_boss_is_refused(self):
        problem = mcmaster.hardware_problem(
            self.BEARING, None, self.sizes(bar_width=8.0))
        self.assertIn('exceeds bar_width', problem)

    def test_manual_edits_that_contradict_fetched_parts_are_refused(self):
        self.assertIn('bearing_OD', mcmaster.hardware_problem(
            None, self.BUSHING, self.sizes(bearing_OD=9.0)))
        self.assertIn('bearing_thickness', mcmaster.hardware_problem(
            self.BEARING, None, self.sizes(bearing_thickness=2.0)))

    def test_mismatched_bores_are_refused(self):
        narrow = dict(self.BEARING, id_mm=5.0)
        self.assertIn('bore', mcmaster.hardware_problem(
            narrow, self.BUSHING, self.sizes()))


class OfflineFetchTests(IsolatedCase):

    def drop_download(self, name, od, id_, length):
        path = os.path.join(self.downloads, name)
        with open(path, 'wb') as fh:
            fh.write(make_step(od, id_, length))
        return path

    def test_download_is_picked_up_measured_and_cached(self):
        self.drop_download('7806K58_Ball Thrust Bearing.STEP', 12.0, 6.0, 4.5)
        with NoNetwork():
            dims, source = mcmaster.fetch_hardware('7806k58', 'bearing')
        self.assertEqual(source, 'step')
        self.assertAlmostEqual(dims['od_mm'], 12.0, places=6)
        self.assertAlmostEqual(dims['id_mm'], 6.0, places=6)
        self.assertAlmostEqual(dims['thickness_mm'], 4.5, places=6)
        self.assertEqual(dims['description'], 'Ball Thrust Bearing')
        self.assertIn('7806K58.step', self.cache_files())
        # Second lookup comes from the cache, even with Downloads emptied.
        os.remove(os.path.join(self.downloads, '7806K58_Ball Thrust Bearing.STEP'))
        with NoNetwork():
            again, source = mcmaster.fetch_hardware('7806K58', 'bearing')
        self.assertEqual(source, 'cache')
        self.assertEqual(again, dims)

    def test_partial_number_never_matches_a_longer_download(self):
        self.drop_download('7806K58_Ball Thrust Bearing.STEP', 12.0, 6.0, 4.5)
        self.assertIsNone(mcmaster.find_downloaded_step('7806K5'))
        self.assertIsNone(mcmaster.find_downloaded_step('7'))
        with NoNetwork(), self.assertRaises(mcmaster.McMasterError):
            mcmaster.fetch_hardware('7806K5', 'bearing', local_only=True)
        self.assertNotIn('7806K5.step', self.cache_files())

    def test_browser_duplicate_name_is_matched(self):
        self.drop_download('6658K727 (1).step', 10.0, 6.0, 6.0)
        with NoNetwork():
            dims, source = mcmaster.fetch_hardware('6658K727', 'bushing')
        self.assertEqual(source, 'step')
        self.assertAlmostEqual(dims['length_mm'], 6.0, places=6)

    def test_incomplete_number_is_refused_without_lookup(self):
        with NoNetwork() as net, self.assertRaises(mcmaster.McMasterError) as ctx:
            mcmaster.fetch_hardware('780', 'bearing')
        self.assertIn('not a complete McMaster part number', str(ctx.exception))
        self.assertEqual(net.calls, [])

    def test_nothing_available_explains_both_options_offline(self):
        with NoNetwork() as net, self.assertRaises(mcmaster.McMasterError) as ctx:
            mcmaster.fetch_hardware(BEARING_PN, 'bearing')
        message = str(ctx.exception)
        self.assertIn('https://www.mcmaster.com/7806K58/', message)
        self.assertIn(self.downloads, message)
        self.assertEqual(net.calls, [])
        with NoNetwork(), self.assertRaises(mcmaster.McMasterError):
            mcmaster.ensure_step(BEARING_PN)


# ---------------------------------------------------------------------------
# Online behaviour (the official API, against the stand-in)

@unittest.skipUnless(shutil.which('openssl'), 'openssl CLI needed for test certificates')
class OnlineApiTests(IsolatedCase):

    @classmethod
    def setUpClass(cls):
        cls.certs = tempfile.mkdtemp(prefix='ssm-mcm-certs-')
        cls.client_pem = make_certificates(cls.certs)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(os.path.join(cls.certs, 'server.pem'),
                                os.path.join(cls.certs, 'server.key'))
        context.verify_mode = ssl.CERT_REQUIRED      # McMaster: cert on every request
        context.load_verify_locations(os.path.join(cls.certs, 'ca.pem'))
        cls.handler = type('BoundHandler', (Handler,), {})
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), cls.handler)
        cls.server.socket = context.wrap_socket(cls.server.socket, server_side=True)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = 'https://localhost:%d' % cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        shutil.rmtree(cls.certs, ignore_errors=True)

    def setUp(self):
        super().setUp()
        self.api = self.handler.api = FakeApi()
        mcmaster.API_BASE = self.base
        os.environ['SSL_CERT_FILE'] = os.path.join(self.certs, 'ca.pem')
        self.write_config()

    def write_config(self, **changes):
        config = {'certificate': self.client_pem, 'certificate_password': '',
                  'username': USER, 'password': PASSWORD}
        config.update(changes)
        with open(os.path.join(self.tmp, mcmaster.CONFIG_BASENAME), 'w') as fh:
            json.dump(config, fh)
        mcmaster._session = None

    def test_fetch_reads_dimensions_from_the_spec_table(self):
        dims, source = mcmaster.fetch_hardware(BEARING_PN, 'bearing')
        self.assertEqual(source, 'api')
        self.assertAlmostEqual(dims['od_mm'], 12.0)
        self.assertAlmostEqual(dims['id_mm'], 6.0)
        self.assertAlmostEqual(dims['thickness_mm'], 4.5)
        self.assertEqual(dims['description'], 'Ball Thrust Bearing')
        bushing, _ = mcmaster.fetch_hardware(BUSHING_PN, 'bushing')
        self.assertAlmostEqual(bushing['od_mm'], 10.0)
        self.assertAlmostEqual(bushing['length_mm'], 6.0)
        # One login reused for both parts; Fetch never downloads CAD.
        self.assertEqual(self.api.logins, 1)
        self.assertEqual([m for m, _p in self.api.requests], ['POST', 'PUT', 'PUT'])
        self.assertNotIn('7806K58.step', self.cache_files())

    def test_generate_downloads_the_step_once_then_uses_the_cache(self):
        mcmaster.fetch_hardware(BEARING_PN, 'bearing')
        path = mcmaster.ensure_step(BEARING_PN)
        self.assertEqual(os.path.basename(path), '7806K58.step')
        with open(path, 'rb') as fh:
            self.assertEqual(fh.read(), CAD[STEP_LINK[BEARING_PN]])
        # The cached product JSON supplied the CAD link: no second PUT.
        self.assertEqual([m for m, _p in self.api.requests], ['POST', 'PUT', 'GET'])
        self.assertEqual(self.api.requests[-1][1], STEP_LINK[BEARING_PN])
        # The downloaded STEP measures the same as the spec table says.
        measured = mcmaster.dims_from_step(path, 'bearing')
        self.assertAlmostEqual(measured['od_mm'], 12.0, places=6)
        self.assertAlmostEqual(measured['thickness_mm'], 4.5, places=6)
        # From here on nothing touches the network, even with no API at all.
        os.remove(os.path.join(self.tmp, mcmaster.CONFIG_BASENAME))
        mcmaster._session = None
        with NoNetwork() as net:
            self.assertEqual(mcmaster.ensure_step(BEARING_PN), path)
            self.assertEqual(mcmaster.fetch_hardware(BEARING_PN, 'bearing')[1], 'cache')
        self.assertEqual(net.calls, [])

    def test_ensure_step_without_prior_fetch(self):
        path = mcmaster.ensure_step(BUSHING_PN)
        self.assertTrue(os.path.isfile(path))
        self.assertEqual([m for m, _p in self.api.requests], ['POST', 'PUT', 'GET'])

    def test_expired_token_logs_in_again_and_retries(self):
        mcmaster.fetch_hardware(BEARING_PN, 'bearing')
        self.api.expire_all_tokens()       # 24 h later, same Fusion session
        dims, source = mcmaster.fetch_hardware(BUSHING_PN, 'bushing')
        self.assertEqual(source, 'api')
        self.assertAlmostEqual(dims['od_mm'], 10.0)
        self.assertEqual(self.api.logins, 2)

    def test_wrong_password_is_reported(self):
        self.write_config(password='wrong')
        with self.assertRaises(mcmaster.McMasterError) as ctx:
            mcmaster.fetch_hardware(BEARING_PN, 'bearing')
        self.assertIn('login failed', str(ctx.exception))
        self.assertIn('incorrect', str(ctx.exception))

    def test_unknown_part_is_reported(self):
        with self.assertRaises(mcmaster.McMasterError) as ctx:
            mcmaster.fetch_hardware('9999K99', 'bearing')
        self.assertIn('400', str(ctx.exception))
        self.assertIn('9999K99', str(ctx.exception))
        self.assertNotIn('9999K99.dims.json', self.cache_files())

    def test_part_without_step_is_reported_at_generate(self):
        dims, _ = mcmaster.fetch_hardware(NO_CAD_PN, 'bearing')
        self.assertAlmostEqual(dims['thickness_mm'], 3.175)
        with self.assertRaises(mcmaster.McMasterError) as ctx:
            mcmaster.ensure_step(NO_CAD_PN)
        self.assertIn('no 3-D STEP', str(ctx.exception))

    def test_wrong_kind_is_reported(self):
        with self.assertRaises(mcmaster.McMasterError) as ctx:
            mcmaster.fetch_hardware(BEARING_PN, 'bushing')   # no Length attribute
        self.assertIn('sleeve bushing', str(ctx.exception))

    def test_missing_certificate_file_is_reported_before_any_request(self):
        self.write_config(certificate=os.path.join(self.tmp, 'nope.pem'))
        with self.assertRaises(mcmaster.McMasterError) as ctx:
            mcmaster.fetch_hardware(BEARING_PN, 'bearing')
        self.assertIn('Certificate file not found', str(ctx.exception))
        self.assertEqual(self.api.requests, [])

    def test_server_refuses_a_client_without_the_certificate(self):
        # Sanity check on the stand-in itself: like McMaster's, it must not
        # answer anyone who does not present the client certificate.
        context = ssl.create_default_context(cafile=os.path.join(self.certs, 'ca.pem'))
        request = urllib.request.Request(self.base + '/v1/login', method='POST',
                                         data=b'{}')
        with self.assertRaises((ssl.SSLError, urllib.error.URLError,
                                ConnectionError, OSError)):
            urllib.request.urlopen(request, context=context, timeout=5).read()
        self.assertEqual(self.api.requests, [])

    def test_local_only_never_calls_the_api(self):
        with NoNetwork() as net, self.assertRaises(mcmaster.McMasterError):
            mcmaster.fetch_hardware(BEARING_PN, 'bearing', local_only=True)
        self.assertEqual(net.calls, [])


if __name__ == '__main__':
    unittest.main()
