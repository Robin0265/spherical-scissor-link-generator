"""McMaster-Carr part lookup for the hardware parameters.

Talks to the official Product Information API (https://www.mcmaster.com/help/api):
log in with a client certificate + username/password, PUT the part number to
subscribe and receive its specification table, GET the 3-D STEP into the local
cache. The anonymous website endpoints are NOT used: they validate per-request
tokens minted by the site's own JavaScript (X-MCM-T-ID and friends, 403
without them), which is bot protection this tool does not try to defeat.

API access is free for McMaster customers but must be requested once:
email eprocurement@mcmaster.com, receive a client certificate, and describe
it in a config file (see CONFIG_TEMPLATE below and the README).

This module never imports adsk, so everything in it can be exercised from a
plain interpreter; the dialog wiring (worker thread, custom event) lives in
command.py. Network results are cached under CACHE_DIRNAME so re-runs and the
solid stage never depend on the network being up.
"""

import json
import os
import re
import ssl
import urllib.error
import urllib.request

API_BASE = 'https://api.mcmaster.com'
TIMEOUT_S = 20
CACHE_DIRNAME = '.mcmaster-cache'
CONFIG_BASENAME = 'mcmaster_credentials.json'

CONFIG_TEMPLATE = {
    'certificate': 'C:/path/to/mcmaster-client-cert.pem',
    'certificate_password': 'password McMaster sent with the certificate',
    'username': 'you@example.com',
    'password': 'your mcmaster.com password',
}


class McMasterError(RuntimeError):
    """A fetch problem with a message fit for the dialog's status line."""


# ---------------------------------------------------------------------------
# Config + cache locations

def _repo_root():
    """The folder that contains SphericalScissorGenerator (the repo checkout)."""
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def config_paths():
    """Where credentials are looked for, in order."""
    return [
        os.path.join(_repo_root(), CONFIG_BASENAME),
        os.path.join(os.path.expanduser('~'), CONFIG_BASENAME),
    ]


def load_config():
    """The first readable config, or None if the API is not set up."""
    for path in config_paths():
        if os.path.isfile(path):
            try:
                with open(path, 'r', encoding='utf-8') as fh:
                    cfg = json.load(fh)
            except (OSError, ValueError) as err:
                raise McMasterError('Could not read %s: %s' % (path, err))
            missing = [k for k in ('certificate', 'username', 'password')
                       if not cfg.get(k)]
            if missing:
                raise McMasterError(
                    '%s is missing %s.' % (path, ', '.join(missing)))
            if not os.path.isfile(cfg['certificate']):
                raise McMasterError(
                    'Certificate file not found: %s' % cfg['certificate'])
            return cfg
    return None


def cache_dir():
    path = os.path.join(_repo_root(), CACHE_DIRNAME)
    os.makedirs(path, exist_ok=True)
    return path


def cached_product(part_number):
    """The cached spec JSON for a part, or None."""
    path = os.path.join(cache_dir(), part_number.upper() + '.json')
    if os.path.isfile(path):
        try:
            with open(path, 'r', encoding='utf-8') as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return None
    return None


def cached_step(part_number):
    """Path of the cached STEP file for a part, or None."""
    path = os.path.join(cache_dir(), part_number.upper() + '.step')
    return path if os.path.isfile(path) else None


# ---------------------------------------------------------------------------
# API session

class Session:
    """One logged-in API session. Reuse it: tokens live for 24 h."""

    def __init__(self, config):
        self._config = config
        self._token = None
        context = ssl.create_default_context()
        try:
            context.load_cert_chain(
                config['certificate'],
                keyfile=config.get('certificate_key') or None,
                password=config.get('certificate_password') or None)
        except (ssl.SSLError, OSError) as err:
            raise McMasterError('Could not load the client certificate: %s' % err)
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=context))

    def _request(self, method, path, body=None, want_bytes=False):
        request = urllib.request.Request(API_BASE + path, method=method)
        request.add_header('Content-Type', 'application/json')
        if self._token:
            request.add_header('Authorization', 'Bearer ' + self._token)
        data = json.dumps(body).encode('utf-8') if body is not None else None
        try:
            with self._opener.open(request, data=data, timeout=TIMEOUT_S) as resp:
                payload = resp.read()
        except urllib.error.HTTPError as err:
            raise McMasterError(_readable_http_error(err))
        except urllib.error.URLError as err:
            raise McMasterError('Could not reach api.mcmaster.com: %s' % err.reason)
        if want_bytes:
            return payload
        if not payload:
            return None
        try:
            return json.loads(payload.decode('utf-8'))
        except ValueError:
            raise McMasterError('api.mcmaster.com sent an unreadable response.')

    def login(self):
        reply = self._request('POST', '/v1/login', {
            'UserName': self._config['username'],
            'Password': self._config['password'],
        })
        token = (reply or {}).get('AuthToken')
        if not token:
            raise McMasterError('Login succeeded but no token was returned.')
        self._token = token

    def product(self, part_number):
        """Subscribe to a part (idempotent) and return its info JSON.

        PUT both subscribes and returns the data, so a part never fetched
        before still works in one round trip.
        """
        if not self._token:
            self.login()
        return self._request('PUT', '/v1/products', {
            'URL': 'https://mcmaster.com/' + part_number,
        })

    def download(self, relative_path):
        """A file (CAD, image, datasheet) from a Links entry, as bytes."""
        if not self._token:
            self.login()
        return self._request('GET', relative_path, want_bytes=True)


def _readable_http_error(err):
    detail = ''
    try:
        body = json.loads(err.read().decode('utf-8'))
        detail = body.get('ErrorDescription') or body.get('ErrorMessage') or ''
    except Exception:
        pass
    if err.code == 401:
        return 'McMaster API login failed%s' % (': ' + detail if detail else '.')
    return 'McMaster API error %d%s' % (err.code, ': ' + detail if detail else '')


# Module-level session so repeated fetches from the dialog reuse the token.
_session = None


def fetch_product(part_number, refresh=False):
    """Product info for a part number, from cache or the API.

    Returns (product_json, source) where source is 'cache' or 'api'. Raises
    McMasterError with a dialog-ready message when neither works.
    """
    global _session
    part_number = part_number.strip().upper()
    if not part_number:
        raise McMasterError('Enter a part number first.')
    if not refresh:
        cached = cached_product(part_number)
        if cached is not None:
            return cached, 'cache'

    config = load_config()
    if config is None:
        raise McMasterError(
            'Part %s is not cached and the McMaster API is not configured.\n'
            'Create %s (see the README) or enter the dimensions by hand.'
            % (part_number, config_paths()[0]))
    if _session is None:
        _session = Session(config)
    product = _session.product(part_number)
    if not isinstance(product, dict) or 'PartNumber' not in product:
        raise McMasterError('Unexpected reply for part %s.' % part_number)

    path = os.path.join(cache_dir(), part_number + '.json')
    with open(path, 'w', encoding='utf-8') as fh:
        json.dump(product, fh, indent=1)
    return product, 'api'


def fetch_step(part_number, product):
    """Download the part's 3-D STEP into the cache. Returns the path.

    Failures are the caller's to soften: specs without a STEP are still
    useful, so this must not take the autofill down with it.
    """
    existing = cached_step(part_number.upper())
    if existing:
        return existing
    link = None
    for entry in product.get('Links', []):
        if 'STEP' in (entry.get('Key') or '').upper():
            link = entry.get('Value')
            break
    if not link:
        raise McMasterError('Part %s has no 3-D STEP file.' % part_number)
    global _session
    if _session is None:
        config = load_config()
        if config is None:
            raise McMasterError('The McMaster API is not configured.')
        _session = Session(config)
    payload = _session.download(link)
    path = os.path.join(cache_dir(), part_number.upper() + '.step')
    with open(path, 'wb') as fh:
        fh.write(payload)
    return path


# ---------------------------------------------------------------------------
# Spec parsing

_FRACTION = re.compile(r'^(?:(\d+)[\s-])?(\d+)/(\d+)$')
_DECIMAL = re.compile(r'^\d+(?:\.\d+)?$')


def parse_length_mm(text):
    """A McMaster dimension string as millimetres, or None.

    Handles the catalogue's habits: '1/4"', '1-3/4"', '0.257"', '5 mm',
    '5mm', '13/16"'. Anything non-dimensional (materials, trade numbers)
    returns None rather than raising, because callers scan attribute lists.
    """
    if not text:
        return None
    value = text.strip().replace('\u2033', '"').replace('\u201d', '"')
    unit = None
    if value.endswith('"'):
        unit, value = 'in', value[:-1].strip()
    elif value.lower().endswith('mm'):
        unit, value = 'mm', value[:-2].strip()
    elif value.lower().endswith('cm'):
        unit, value = 'cm', value[:-2].strip()

    number = None
    match = _FRACTION.match(value)
    if match:
        whole = int(match.group(1) or 0)
        number = whole + int(match.group(2)) / int(match.group(3))
        unit = unit or 'in'  # bare fractions are catalogue inches
    elif _DECIMAL.match(value):
        number = float(value)
    if number is None or unit is None:
        return None
    return number * {'in': 25.4, 'mm': 1.0, 'cm': 10.0}[unit]


def spec_value_mm(product, attribute_names):
    """The first parseable dimension among the named attributes, else None.

    Comparison ignores case and punctuation, so 'OD', 'O.D.' and 'Od' all
    meet an 'od' candidate.
    """
    def norm(name):
        return re.sub(r'[^a-z0-9]', '', (name or '').lower())

    wanted = [norm(name) for name in attribute_names]
    for spec in product.get('Specifications', []):
        if norm(spec.get('Attribute')) in wanted:
            for value in spec.get('Values', []):
                parsed = parse_length_mm(value)
                if parsed is not None:
                    return parsed
    return None


def extract_bearing(product):
    """OD / ID / thickness (mm) of a thrust bearing's spec table."""
    dims = {
        'od_mm': spec_value_mm(product, ['OD', 'Outside Diameter']),
        'id_mm': spec_value_mm(product, ['ID', 'Inside Diameter', 'For Shaft Diameter']),
        'thickness_mm': spec_value_mm(product, ['Thickness', 'Overall Thickness',
                                                'Height', 'Overall Height']),
        'description': product.get('FamilyDescription') or '',
    }
    if dims['thickness_mm'] is None:
        raise McMasterError(
            'Part %s has no thickness attribute - is it a thrust bearing?'
            % product.get('PartNumber'))
    return dims


def extract_bushing(product):
    """OD / ID / length (mm) of a sleeve bushing's spec table."""
    dims = {
        'od_mm': spec_value_mm(product, ['OD', 'Outside Diameter']),
        'id_mm': spec_value_mm(product, ['ID', 'Inside Diameter', 'For Shaft Diameter']),
        'length_mm': spec_value_mm(product, ['Length', 'Overall Length']),
        'description': product.get('FamilyDescription') or '',
    }
    if dims['od_mm'] is None or dims['length_mm'] is None:
        raise McMasterError(
            'Part %s has no OD/length attributes - is it a sleeve bushing?'
            % product.get('PartNumber'))
    return dims


# ---------------------------------------------------------------------------
# Autofill

def hardware_autofill(bearing, bushing, bar_thickness_mm, bar_width_mm):
    """Parameter expressions implied by the fetched parts.

    Only bearing_OD is a straight copy (the boss bore takes the press-fit
    bushing's OD). l_offset is derived from the generator's own boss recipe:
    the boss extends bar_thickness + 2*l_offset symmetrically, and the
    bushing must sit flush through it, so l_offset makes the boss height
    equal the bushing length. bearing_thickness is the thrust bearing's
    thickness (the gap the solids stage leaves between mating bosses).

    Returns (expressions, warnings): expressions is {param name: 'x mm'},
    warnings is a list of readable strings for the status line. A geometric
    impossibility (bushing shorter than the bar) raises instead.
    """
    expressions = {}
    warnings = []

    if bearing is not None:
        expressions['bearing_thickness'] = '%.4g mm' % bearing['thickness_mm']

    if bushing is not None:
        expressions['bearing_OD'] = '%.4g mm' % bushing['od_mm']
        spare = bushing['length_mm'] - bar_thickness_mm
        if spare <= 0:
            raise McMasterError(
                'Bushing length (%.3g mm) must exceed bar_thickness (%.3g mm) '
                'for the boss to hold it - pick a longer bushing or a thinner bar.'
                % (bushing['length_mm'], bar_thickness_mm))
        expressions['l_offset'] = '%.4g mm' % (spare / 2.0)

    if bearing is not None and bearing['od_mm'] is not None \
            and bearing['od_mm'] > bar_width_mm:
        warnings.append(
            'Thrust bearing OD (%.3g mm) overhangs the boss (bar_width %.3g mm).'
            % (bearing['od_mm'], bar_width_mm))
    if bearing is not None and bushing is not None \
            and bearing['id_mm'] is not None and bushing['id_mm'] is not None \
            and bearing['id_mm'] < bushing['id_mm']:
        warnings.append(
            'Thrust bearing bore (%.3g mm) is smaller than the bushing bore '
            '(%.3g mm) - the pin will not pass through both.'
            % (bearing['id_mm'], bushing['id_mm']))
    return expressions, warnings
