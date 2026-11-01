"""McMaster-Carr part lookup for the hardware parameters.

Three sources, tried in order per part number:

1. The local cache (CACHE_DIRNAME) - dimensions measured or fetched before.
2. The official Product Information API (https://www.mcmaster.com/help/api),
   when credentials are configured - optional, see CONFIG_TEMPLATE.
3. A STEP file the user downloaded from mcmaster.com by hand: Fetch opens the
   part page in the browser, the user clicks the 3-D STEP download (free and
   anonymous - it is how McMaster intends CAD to be retrieved), and the next
   Fetch picks the file up from Downloads and MEASURES the dimensions from
   its geometry: bore and outside diameter from the cylindrical faces along
   the dominant axis, length/thickness from the extent along that axis. No
   credentials, no scraping.

The site's anonymous data endpoints are NOT used: they validate per-request
tokens minted by the site's own JavaScript (X-MCM-T-ID and friends, 403
without them), which is bot protection this tool does not try to defeat.

This module never imports adsk, so everything in it can be exercised from a
plain interpreter; the dialog wiring (worker thread, custom event) lives in
command.py. Everything retrieved lands in the cache, so re-runs and the solid
stage never depend on the network being up.
"""

import json
import os
import re
import shutil
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


def part_url(part_number):
    return 'https://www.mcmaster.com/%s/' % part_number


# McMaster part numbers are digits, a catalogue letter (sometimes two), then
# digits: 7806K58, 6658K727, 91251A540. Anything else - in particular a
# number still being typed ('7', '780', '7806K') - is not looked up at all.
_PART_NUMBER = re.compile(r'^\d{3,6}[A-Z]{1,2}\d{1,5}$')


def is_part_number(part_number):
    return bool(_PART_NUMBER.match((part_number or '').strip().upper()))


def _require_part_number(part_number):
    if not is_part_number(part_number):
        raise McMasterError(
            '"%s" is not a complete McMaster part number (e.g. 7806K58).'
            % part_number)


# ---------------------------------------------------------------------------
# Config + cache locations

def _repo_root():
    """The folder that contains SphericalScissorGenerator (the repo checkout)."""
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def config_paths():
    """Where API credentials are looked for, in order."""
    return [
        os.path.join(_repo_root(), CONFIG_BASENAME),
        os.path.join(os.path.expanduser('~'), CONFIG_BASENAME),
    ]


def load_config():
    """The first readable config, or None: the API is optional."""
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


def cached_dims(part_number):
    """The cached dimensions for a part, or None."""
    path = os.path.join(cache_dir(), part_number.upper() + '.dims.json')
    if os.path.isfile(path):
        try:
            with open(path, 'r', encoding='utf-8') as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return None
    return None


def save_dims(part_number, dims):
    path = os.path.join(cache_dir(), part_number.upper() + '.dims.json')
    with open(path, 'w', encoding='utf-8') as fh:
        json.dump(dims, fh, indent=1)


def cached_step(part_number):
    """Path of the cached STEP file for a part, or None."""
    path = os.path.join(cache_dir(), part_number.upper() + '.step')
    return path if os.path.isfile(path) else None


def cached_product(part_number):
    """The cached API spec JSON for a part, or None."""
    path = os.path.join(cache_dir(), part_number.upper() + '.json')
    if os.path.isfile(path):
        try:
            with open(path, 'r', encoding='utf-8') as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return None
    return None


def downloads_dirs():
    """Every folder a browser download can land in, most authoritative first.

    Windows keeps the real Downloads location in the registry (it is often
    redirected, e.g. to OneDrive, on managed machines), so ask there first
    and fall back to the conventional spots.
    """
    folders = []
    try:
        import winreg
        with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r'Software\Microsoft\Windows\CurrentVersion\Explorer'
                r'\User Shell Folders') as key:
            value = winreg.QueryValueEx(
                key, '{374DE290-123F-4565-9164-39C4925E467B}')[0]
            folders.append(os.path.expandvars(value))
    except Exception:
        pass
    folders.append(os.path.join(os.path.expanduser('~'), 'Downloads'))
    onedrive = os.environ.get('OneDrive')
    if onedrive:
        folders.append(os.path.join(onedrive, 'Downloads'))
    unique = []
    for folder in folders:
        if folder not in unique and os.path.isdir(folder):
            unique.append(folder)
    return unique


def names_part(stem, part_number):
    """True when a file stem is exactly this part number, optionally followed
    by a separator and a description ('7806K58', '7806K58_Thrust Bearing',
    '7806K58 (1)'). A bare prefix test is NOT enough: '7' and '7806K5' are
    prefixes of '7806K58', which is how every keystroke of a part number
    being typed used to "find" the finished download and cache it under the
    partial number."""
    stem, part_number = stem.upper(), part_number.strip().upper()
    if stem == part_number:
        return True
    return stem.startswith(part_number) and stem[len(part_number)] in '_ -.('


_names_part = names_part  # older name


def description_from_name(name):
    """'7806K58_Ball Thrust Bearing' -> 'Ball Thrust Bearing' ('' if none).
    McMaster names downloads and Fusion's inserted components this way."""
    return name.split('_', 1)[1].strip() if '_' in name else ''


def find_downloaded_step(part_number, folder=None):
    """The newest '<PN>[_description].step' in the Downloads folder(s), or None.

    McMaster names its downloads '<PN>_<FAMILY DESCRIPTION>.STEP'; the part
    number must match exactly up to the separator (see _names_part).
    """
    part_number = part_number.strip().upper()
    if not is_part_number(part_number):
        return None
    candidates = []
    for place in ([folder] if folder else downloads_dirs()):
        if not os.path.isdir(place):
            continue
        for name in os.listdir(place):
            stem, ext = os.path.splitext(name)
            if ext.upper() in ('.STEP', '.STP') and names_part(stem, part_number):
                candidates.append(os.path.join(place, name))
    if not candidates:
        return None
    candidates.sort(key=os.path.getmtime, reverse=True)
    return candidates[0]


# ---------------------------------------------------------------------------
# Measuring a STEP file
#
# McMaster's bearings and bushings are solids of revolution (plus rollers in
# a bearing's cage): every dimension the autofill needs is a cylinder radius
# or an extent along the part's axis, and STEP is a readable text format, so
# no CAD kernel is needed. The dominant axis is the largest cylinder's; only
# cylinders aligned with it count (a needle bearing's rollers are cylinders
# too, but they lie crosswise and must not be mistaken for a bore).

# STEP writes floats as '0.', '12.7' or '1.E-3', so the mantissa must accept
# a bare trailing dot.
_NUM = r'[-+]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[Ee][-+]?[0-9]+)?'


def _step_unit_scale(text):
    """Millimetres per model unit, from the file's unit declaration."""
    if re.search(r"CONVERSION_BASED_UNIT\s*\(\s*'INCH", text, re.IGNORECASE):
        return 25.4
    if re.search(r'SI_UNIT\s*\(\s*\.MILLI\.\s*,\s*\.METRE\.\s*\)', text):
        return 1.0
    if re.search(r'SI_UNIT\s*\(\s*\.CENTI\.\s*,\s*\.METRE\.\s*\)', text):
        return 10.0   # Fusion's own STEP export can declare centimetres
    if re.search(r'SI_UNIT\s*\(\s*\$?\s*,\s*\.METRE\.\s*\)', text):
        return 1000.0
    return 1.0  # McMaster exports declare units; a bare file is assumed mm


def dims_from_step(path, kind):
    """Measure {od_mm, id_mm, thickness_mm|length_mm} from a STEP file.

    kind is 'bearing' (axial extent reported as thickness) or 'bushing'
    (reported as length). Raises McMasterError when the file has no
    cylindrical faces to measure - the wrong kind of part.
    """
    with open(path, 'r', encoding='utf-8', errors='replace') as fh:
        text = fh.read()
    scale = _step_unit_scale(text)

    directions = {}
    for ent_id, coords in re.findall(
            r"#(\d+)\s*=\s*DIRECTION\s*\(\s*'[^']*'\s*,\s*\(([^)]*)\)", text):
        parts = [float(v) for v in re.findall(_NUM, coords)]
        if len(parts) == 3:
            directions[ent_id] = parts

    # AXIS2_PLACEMENT_3D('', #origin, #axis, #refdir) - the axis is ref 2.
    placement_axis = {}
    for ent_id, refs in re.findall(
            r"#(\d+)\s*=\s*AXIS2_PLACEMENT_3D\s*\(\s*'[^']*'\s*,([^;]*)\)", text):
        ref_ids = re.findall(r'#(\d+)', refs)
        if len(ref_ids) >= 2:
            placement_axis[ent_id] = directions.get(ref_ids[1])

    cylinders = []  # (radius, axis vector or None)
    for placement_id, radius in re.findall(
            r"CYLINDRICAL_SURFACE\s*\(\s*'[^']*'\s*,\s*#(\d+)\s*,\s*(%s)" % _NUM,
            text):
        cylinders.append((float(radius), placement_axis.get(placement_id)))
    if not cylinders:
        raise McMasterError(
            '%s has no cylindrical faces to measure.' % os.path.basename(path))

    axis = max(cylinders, key=lambda c: c[0])[1] or [0.0, 0.0, 1.0]
    norm = sum(c * c for c in axis) ** 0.5 or 1.0
    axis = [c / norm for c in axis]

    def aligned(vector):
        if vector is None:
            return False
        length = sum(c * c for c in vector) ** 0.5 or 1.0
        dot = sum(a * v for a, v in zip(axis, vector)) / length
        return abs(dot) > 0.99

    radii = sorted(radius for radius, vector in cylinders if aligned(vector))
    outer = radii[-1]
    # A distinct smallest aligned radius is the bore; chamfers make cones,
    # not cylinders, so they cannot sneak in here.
    inner = radii[0] if radii[0] < outer * 0.999 else None

    spans = []
    for coords in re.findall(
            r"CARTESIAN_POINT\s*\(\s*'[^']*'\s*,\s*\(([^)]*)\)", text):
        parts = [float(v) for v in re.findall(_NUM, coords)]
        if len(parts) == 3:
            spans.append(sum(a * v for a, v in zip(axis, parts)))
    if not spans:
        raise McMasterError(
            '%s has no coordinate data to measure.' % os.path.basename(path))
    extent = (max(spans) - min(spans)) * scale

    name = os.path.splitext(os.path.basename(path))[0]
    description = description_from_name(name).title()
    dims = {
        'od_mm': 2.0 * outer * scale,
        'id_mm': 2.0 * inner * scale if inner is not None else None,
        'description': description,
    }
    dims['thickness_mm' if kind == 'bearing' else 'length_mm'] = extent
    return dims


# ---------------------------------------------------------------------------
# Optional API session

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

    def _request(self, method, path, body=None, want_bytes=False, retry=True):
        request = urllib.request.Request(API_BASE + path, method=method)
        request.add_header('Content-Type', 'application/json')
        if self._token:
            request.add_header('Authorization', 'Bearer ' + self._token)
        data = json.dumps(body).encode('utf-8') if body is not None else None
        try:
            with self._opener.open(request, data=data, timeout=TIMEOUT_S) as resp:
                payload = resp.read()
        except urllib.error.HTTPError as err:
            category, detail = _http_error_parts(err)
            # Tokens expire after 24 h, and the session lives as long as
            # Fusion does: log in again once and retry, rather than failing
            # every lookup until the add-in is restarted.
            if retry and self._token and path != '/v1/login' \
                    and category in _TOKEN_ERRORS:
                self._token = None
                self.login()
                return self._request(method, path, body, want_bytes, retry=False)
            raise McMasterError(_readable_http_error(err.code, detail))
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


# ErrorMessage categories that mean "log in again", per the API docs.
_TOKEN_ERRORS = ('EXPIRED_AUTHORIZATION_TOKEN', 'INVALID_AUTHORIZATION_TOKEN')


def _http_error_parts(err):
    """(ErrorMessage category, readable detail) from an API error body."""
    try:
        body = json.loads(err.read().decode('utf-8'))
    except Exception:
        return '', ''
    finally:
        err.close()  # the error carries the open response
    category = (body.get('ErrorMessage') or '').upper()
    detail = body.get('ErrorDescription') or body.get('ErrorMessage') or ''
    return category, detail


def _readable_http_error(code, detail):
    if code == 401:
        return 'McMaster API login failed%s' % (': ' + detail if detail else '.')
    return 'McMaster API error %d%s' % (code, ': ' + detail if detail else '')


# Module-level session so repeated fetches from the dialog reuse the token.
_session = None


def _api_session():
    global _session
    if _session is None:
        _session = Session(load_config())
    return _session


def _api_product(part_number):
    """Product info JSON via the API, cached beside the dims."""
    product = _api_session().product(part_number)
    if not isinstance(product, dict) or 'PartNumber' not in product:
        raise McMasterError('Unexpected API reply for part %s.' % part_number)
    path = os.path.join(cache_dir(), part_number + '.json')
    with open(path, 'w', encoding='utf-8') as fh:
        json.dump(product, fh, indent=1)
    return product


def _api_step(part_number, product):
    """Download the part's 3-D STEP into the cache. Returns the path."""
    existing = cached_step(part_number)
    if existing:
        return existing
    link = None
    for entry in product.get('Links', []):
        if 'STEP' in (entry.get('Key') or '').upper():
            link = entry.get('Value')
            break
    if not link:
        raise McMasterError('Part %s has no 3-D STEP file.' % part_number)
    payload = _api_session().download(link)
    path = os.path.join(cache_dir(), part_number + '.step')
    with open(path, 'wb') as fh:
        fh.write(payload)
    return path


# ---------------------------------------------------------------------------
# Spec parsing (API responses)

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
# The lookup itself

def fetch_hardware(part_number, kind, local_only=False):
    """Dimensions for a part: cache, then API (if configured), then a STEP
    the user downloaded by hand.

    Returns (dims, source) with source 'cache', 'api' or 'step'. Never opens
    a browser. When nothing is available a McMasterError explains the two
    honest options (configure the free API, or drop the STEP in Downloads).

    local_only=True restricts to the cache and Downloads - no network - so
    the dialog can auto-resolve on every keystroke without blocking the UI
    thread.
    """
    part_number = part_number.strip().upper()
    kind = kind.lower()
    if not part_number:
        raise McMasterError('Enter a part number first.')
    if kind not in ('bearing', 'bushing'):
        raise ValueError('kind must be bearing or bushing')
    _require_part_number(part_number)

    dims = cached_dims(part_number)
    if dims is not None:
        return dims, 'cache'

    config = None if local_only else load_config()
    if config is not None:
        # Metadata only - the STEP is deliberately NOT downloaded here.
        # Fetch answers "what are the dimensions"; the CAD is acquired by
        # ensure_step at Generate, where the import actually needs it.
        product = _api_product(part_number)
        extract = extract_bearing if kind == 'bearing' else extract_bushing
        dims = extract(product)
        save_dims(part_number, dims)
        return dims, 'api'

    step = cached_step(part_number)
    measured = step
    if step is None:
        downloaded = find_downloaded_step(part_number)
        if downloaded is not None:
            step = os.path.join(cache_dir(), part_number + '.step')
            shutil.copyfile(downloaded, step)
            # Measure the original: its name ('<PN>_<description>.STEP')
            # carries the description, which the cache copy's name does not.
            measured = downloaded
    if step is not None:
        dims = dims_from_step(measured, kind)
        save_dims(part_number, dims)
        return dims, 'step'

    if local_only:
        # Quiet path (per-keystroke auto-resolve): nothing local yet.
        raise McMasterError('%s %s not available locally yet.'
                            % (kind.capitalize(), part_number))
    raise McMasterError(
        '%s %s: download its 3-D STEP from %s into %s, then it is picked up '
        'automatically (press Fetch or Generate). Automatic download instead '
        'needs the free McMaster API - see the README.'
        % (kind.capitalize(), part_number, part_url(part_number),
           ' or '.join(downloads_dirs()) or 'Downloads'))


def ensure_step(part_number):
    """The part's STEP file in the cache, acquiring it if needed.

    Order: already cached -> a file the user downloaded (copied into the
    cache) -> the API (when configured). This runs at Generate, which is the
    moment the CAD is actually needed - Fetch never touches CAD.
    """
    part_number = part_number.strip().upper()
    _require_part_number(part_number)
    step = cached_step(part_number)
    if step is not None:
        return step
    downloaded = find_downloaded_step(part_number)
    if downloaded is not None:
        step = os.path.join(cache_dir(), part_number + '.step')
        shutil.copyfile(downloaded, step)
        return step
    if load_config() is not None:
        product = cached_product(part_number)
        if product is None:
            product = _api_product(part_number)
        return _api_step(part_number, product)
    raise McMasterError(
        '%s: no STEP available. Download it from %s into %s and re-run '
        'Generate, or configure the free McMaster API for automatic '
        'download (see the README).'
        % (part_number, part_url(part_number),
           ' or '.join(downloads_dirs()) or 'Downloads'))


# ---------------------------------------------------------------------------
# Autofill

def hardware_autofill(bearing, bushing, bar_thickness_mm, bar_width_mm):
    """Parameter expressions implied by the fetched parts.

    bearing_OD is a straight copy (the boss bore takes the press-fit
    bushing's OD) and bearing_thickness is the thrust bearing's thickness
    (the gap the solids stage leaves between mating bosses).

    The bushing sits INSIDE the bar thickness, centred on the bar (the
    hardware stage centres it in the boss, and the boss is symmetric about
    the bar), so its length is a limit - length <= bar_thickness - not a
    source for any parameter: l_offset stays whatever the user set.
    hardware_problem enforces the limit.

    Returns (expressions, warnings): expressions is {param name: 'x mm'},
    warnings is a list of readable strings for the status line.
    """
    expressions = {}
    warnings = []

    if bearing is not None:
        expressions['bearing_thickness'] = '%.6g mm' % bearing['thickness_mm']

    if bushing is not None:
        expressions['bearing_OD'] = '%.6g mm' % bushing['od_mm']

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


# Autofill writes 6 significant figures, so a fetched part and the parameter
# it drove agree to well under this. Anything past it is a manual edit.
_MATCH_TOL_MM = 0.02


def hardware_problem(bearing, bushing, sizes_mm):
    """Why the fetched parts conflict with the current geometry, or None.

    Unlike hardware_autofill's warnings, these are hard stops: the dialog
    feeds the result into its validation, so Generate/Update stays disabled
    until the conflict is resolved (change the geometry, re-Fetch, or clear
    the part number to go back to manual dimensions).

    sizes_mm carries the *current* dialog values in mm: bar_width,
    bar_thickness, l_offset, bearing_OD, bearing_thickness.
    """
    if bearing is not None:
        if bearing.get('od_mm') is not None \
                and bearing['od_mm'] > sizes_mm['bar_width'] + _MATCH_TOL_MM:
            return ('Thrust bearing OD (%.3g mm) exceeds bar_width (the boss '
                    'diameter, %.3g mm) - pick a smaller bearing or widen the '
                    'bar.' % (bearing['od_mm'], sizes_mm['bar_width']))
        if abs(bearing['thickness_mm'] - sizes_mm['bearing_thickness']) > _MATCH_TOL_MM:
            return ('bearing_thickness (%.4g mm) does not match thrust bearing '
                    '%s (%.4g mm) - press Fetch to re-apply, or clear the part '
                    'number for manual dimensions.'
                    % (sizes_mm['bearing_thickness'],
                       bearing.get('description') or 'part',
                       bearing['thickness_mm']))
    if bushing is not None:
        # The bushing presses into a bore in the boss, and the boss diameter
        # is bar_width - so the bushing must be narrower than the bar or the
        # bore cuts straight through the boss wall.
        if bushing['od_mm'] >= sizes_mm['bar_width'] - _MATCH_TOL_MM:
            return ('Bushing OD (%.3g mm) leaves no wall in the boss (bar_width '
                    '= boss diameter, %.3g mm) - widen the bar or pick a '
                    'smaller bushing.' % (bushing['od_mm'], sizes_mm['bar_width']))
        if abs(bushing['od_mm'] - sizes_mm['bearing_OD']) > _MATCH_TOL_MM:
            return ('bearing_OD (the boss bore, %.4g mm) does not match the '
                    'press-fit bushing OD (%.4g mm) - press Fetch to re-apply, '
                    'or clear the part number for manual dimensions.'
                    % (sizes_mm['bearing_OD'], bushing['od_mm']))
        if bushing['length_mm'] > sizes_mm['bar_thickness'] + _MATCH_TOL_MM:
            return ('Bushing length (%.4g mm) exceeds bar_thickness (%.4g mm) - '
                    'the bushing must fit within the bar: pick a shorter '
                    'bushing or a thicker bar.'
                    % (bushing['length_mm'], sizes_mm['bar_thickness']))
    if bearing is not None and bushing is not None \
            and bearing.get('id_mm') is not None and bushing.get('id_mm') is not None \
            and bearing['id_mm'] + _MATCH_TOL_MM < bushing['id_mm']:
        return ('Thrust bearing bore (%.3g mm) is smaller than the bushing '
                'bore (%.3g mm) - the pin cannot pass through both; pick '
                'matching bores.' % (bearing['id_mm'], bushing['id_mm']))
    return None
