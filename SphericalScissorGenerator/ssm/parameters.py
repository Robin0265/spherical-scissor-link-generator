"""The parameter table, expression solving, and validation.

`solve_expressions` evaluates the design without touching the document, so the
dialog can validate input and drive a live preview before anything is created.
"""

import math
import adsk.core

# name, default expression, unit, comment
DRIVING_PARAMS = [
    ('link_Radius', '100 mm', 'mm', 'Radius of the spherical surface tracked'),
    ('beta', '5 deg', 'deg', 'Conflict (bearing intrusion) angle'),
    ('span_max', '135 deg', 'deg', 'Maximum designed spanned angle'),
    ('n', '3', '', 'Number of rhombi'),
    ('span_target', '30 deg', 'deg', 'Spanned angle of this configuration'),
    ('l_offset', '1.5 mm', 'mm', 'Radial clearance each boss protrudes past the bar'),
    ('bar_thickness', '3 mm', 'mm', 'Link bar thickness (radial)'),
    ('bar_width', '8 mm', 'mm', 'Link bar width (lateral); also the boss diameter'),
    ('bearing_OD', '4 mm', 'mm', 'Bearing outer diameter (the bore through each boss)'),
    ('bearing_thickness', '2 mm', 'mm', 'Thrust bearing thickness (gap between mating bosses)'),
]

DERIVED_PARAMS = [
    ('alpha', 'acos(cos(beta) * cos(span_max / ( 2 * n )))', 'deg',
     'Curvature angle of each link'),
    ('delta', 'span_target / n', 'deg', 'Spanned angle carried by one rhombus'),
    ('gamma_', 'acos(cos(alpha) / cos(delta / 2))', 'deg',
     'Pin elevation off the spine plane'),
    ('lambda', 'acos(( cos(alpha) - cos(delta) * cos(alpha) ) / ( sin(delta) * sin(alpha) ))',
     'deg', 'Dihedral angle of the seed link planes about OA'),
]

DEFAULTS = {name: default for name, default, _u, _c in DRIVING_PARAMS}


def _ensure(design, name, expression, units, comment):
    existing = design.userParameters.itemByName(name)
    if existing:
        existing.expression = expression
        if comment:
            try:
                existing.comment = comment
            except Exception:
                pass
        return existing
    return design.userParameters.add(
        name, adsk.core.ValueInput.createByString(expression), units, comment)


def ensure_parameters(design, overrides=None):
    """Create or update the whole table, driving params before derived ones."""
    overrides = overrides or {}
    for name, default, units, comment in DRIVING_PARAMS:
        _ensure(design, name, overrides.get(name, default), units, comment)
    for name, expression, units, comment in DERIVED_PARAMS:
        _ensure(design, name, expression, units, comment)


def remove_legacy(design):
    """Drop parameters earlier versions created that nothing uses any more.

    `sphere_Radius` existed while link_Radius was (wrongly) treated as a
    diameter. deleteMe refuses while something still references the parameter,
    so this is safe to call even on documents that were not purged.
    """
    for name in ('sphere_Radius',):
        stale = design.userParameters.itemByName(name)
        if stale is not None:
            try:
                stale.deleteMe()
            except Exception:
                pass


def read_solved(design):
    """Read back what Fusion actually solved. Angles in radians, lengths in cm."""
    def value(name):
        param = design.userParameters.itemByName(name)
        if param is None:
            raise RuntimeError('Missing parameter "%s".' % name)
        return param.value

    return {
        'R': value('link_Radius'),
        'alpha': value('alpha'),
        'delta': value('delta'),
        'gamma': value('gamma_'),
        'lam': value('lambda'),
        'span_target': value('span_target'),
        'n': int(round(value('n'))),
    }


def solve_expressions(design, overrides=None):
    """Solve the design straight from expressions, without touching the document.

    Mirrors the parameter table's formulas so the dialog can validate and
    preview before anything is created. Raises RuntimeError with a readable
    reason if the expressions or the resulting geometry are not usable.
    """
    overrides = overrides or {}
    units = adsk.core.Application.get().activeProduct.unitsManager
    raw = {}
    for name, default, unit_name, _comment in DRIVING_PARAMS:
        expression = overrides.get(name, DEFAULTS[name])
        try:
            raw[name] = units.evaluateExpression(expression, unit_name)
        except Exception:
            raise RuntimeError('"%s" is not a valid expression for %s.'
                               % (expression, name))

    n = int(round(raw['n']))
    if n < 1:
        raise RuntimeError('n must be at least 1.')
    if raw['link_Radius'] <= 0:
        raise RuntimeError('link_Radius must be positive.')
    if raw['span_target'] <= 0:
        raise RuntimeError('span_target must be positive.')
    if raw['span_max'] <= 0:
        raise RuntimeError('span_max must be positive.')

    alpha_arg = math.cos(raw['beta']) * math.cos(raw['span_max'] / (2 * n))
    if abs(alpha_arg) > 1:
        raise RuntimeError('beta and span_max do not give a real link curvature.')
    alpha = math.acos(alpha_arg)
    delta = raw['span_target'] / n

    if delta / 2 >= alpha:
        raise RuntimeError(
            'The rhombus cannot close: delta/2 (%.2f deg) must stay below alpha '
            '(%.2f deg).\nLower span_target, raise n, or raise span_max.'
            % (math.degrees(delta / 2), math.degrees(alpha)))

    gamma_arg = math.cos(alpha) / math.cos(delta / 2)
    if abs(gamma_arg) > 1:
        raise RuntimeError('No real pin elevation for these parameters.')
    gamma = math.acos(gamma_arg)

    sin_d, sin_a = math.sin(delta), math.sin(alpha)
    if abs(sin_d) < 1e-12 or abs(sin_a) < 1e-12:
        raise RuntimeError('delta and alpha must both be non-zero.')
    lam_arg = (math.cos(alpha) - math.cos(delta) * math.cos(alpha)) / (sin_d * sin_a)
    if abs(lam_arg) > 1:
        raise RuntimeError('No real seed-plane angle for these parameters.')

    return {
        'R': raw['link_Radius'],
        'alpha': alpha,
        'delta': delta,
        'gamma': gamma,
        'lam': math.acos(lam_arg),
        'span_target': raw['span_target'],
        'n': n,
    }


def validate(solved):
    """Re-check an already-solved set (used after Fusion evaluates the table)."""
    if solved['n'] < 1:
        raise RuntimeError('n must be at least 1.')
    if solved['R'] <= 0:
        raise RuntimeError('link_Radius must be positive.')
    if solved['span_target'] <= 0:
        raise RuntimeError('span_target must be positive.')
    if solved['delta'] / 2 >= solved['alpha']:
        raise RuntimeError(
            'The rhombus cannot close: delta/2 (%.2f deg) must stay below alpha '
            '(%.2f deg).\nLower span_target, raise n, or raise span_max.'
            % (math.degrees(solved['delta'] / 2), math.degrees(solved['alpha'])))
    for key in ('alpha', 'gamma', 'lam'):
        if solved[key] != solved[key]:  # NaN
            raise RuntimeError('Parameter "%s" did not evaluate to a real angle.' % key)
