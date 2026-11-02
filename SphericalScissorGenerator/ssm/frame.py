"""Resolving the sphere frame from the user's selections.

The dialog now asks for the sphere centre and an INPUT AXIS - joint A's
revolute axis - only; see resolve_axis. The spine plane is derived: the plane
through the input axis that is as level as the top plane allows (exactly
parallel to it for a horizontal axis), so the spine defaults to running
parallel to the top plane. resolve() below is the original two-plane form,
kept for scripts that still pass planes.

The mechanism needs three directions at the sphere centre:

    eA  centre -> joint A, where the chain starts
    eF  the direction the rhombi fan out (in the spine plane)
    eN  spine-plane normal; the '+' pins sit on the +eN side

Two planes fix all three. The **spine plane** carries the chain. The **start
plane** says where the chain begins: A lies on the line where the two planes
meet, and the spine leaves A perpendicular to the start plane. That perpendicular
relationship is only possible if the start plane is itself perpendicular to the
spine plane, which is checked here rather than left to produce silently wrong
geometry.
"""

import math

from . import vectors as vec


class Frame(object):
    def __init__(self, centre, e_a, e_f):
        self.C = centre
        self.eA = vec.unit(e_a)
        self.eF = vec.unit(e_f)
        self.eN = vec.unit(vec.cross(self.eA, self.eF))

    def joint(self, radius, phi, elevation):
        """Sphere point at in-plane angle `phi` from eA, raised by `elevation`
        toward eN."""
        ce, se = math.cos(elevation), math.sin(elevation)
        cp, sp = math.cos(phi), math.sin(phi)
        return vec.p(
            self.C.x + radius * (ce * (cp * self.eA.x + sp * self.eF.x) + se * self.eN.x),
            self.C.y + radius * (ce * (cp * self.eA.y + sp * self.eF.y) + se * self.eN.y),
            self.C.z + radius * (ce * (cp * self.eA.z + sp * self.eF.z) + se * self.eN.z))


# How close (cm) the centre must be to the input axis, and how close to
# vertical (|sin| of the angle to the up direction) the axis may get before
# the front plane takes over as the levelling reference.
ON_AXIS_TOL = 1e-5
VERTICAL_TOL = 1e-6


def modeling_up():
    """(up, front_normal) unit vectors for Fusion's modeling orientation: the
    top plane's normal, and the front plane's normal used when the input axis
    is itself vertical. Y-up (Fusion's default): top = XZ, front = XY. Z-up:
    top = XY, front = XZ."""
    import adsk.core
    try:
        prefs = adsk.core.Application.get().preferences.generalPreferences
        z_up = (prefs.defaultModelingOrientation
                == adsk.core.DefaultModelingOrientations.ZUpModelingOrientation)
    except Exception:
        z_up = False
    if z_up:
        return vec.v(0, 0, 1), vec.v(0, -1, 0)
    return vec.v(0, 1, 0), vec.v(0, 0, 1)


def spine_normal(e_a, up, front):
    """Normal of the spine plane: the plane through the axis direction e_a
    that is as level as possible - up with its along-axis part removed. A
    vertical axis has no level plane through it, so the front plane's normal
    is used instead."""
    for reference in (up, front):
        along = vec.dot(reference, e_a)
        n = vec.v(reference.x - along * e_a.x, reference.y - along * e_a.y,
                  reference.z - along * e_a.z)
        if math.sqrt(vec.dot(n, n)) > VERTICAL_TOL:
            return vec.unit(n)
    raise RuntimeError('Cannot orient the spine for this input axis.')


def resolve_axis(centre_world, axis_ent, clockwise=False, flip_start=False,
                 up=None, front=None):
    """Frame from the sphere centre and the input axis (joint A's axis).

    A sits on the axis at link_Radius from the centre, in the axis's own
    direction (flip_start: the other way). The spine plane is the levelled
    plane through the axis (spine_normal); the chain fans counter-clockwise
    seen from above (looking down the spine-plane normal) unless clockwise.
    Returns the Frame; frame.eN is the spine-plane normal and frame.eF the
    start-plane normal.
    """
    if up is None or front is None:
        up, front = modeling_up()
    origin, direction = vec.line_of(axis_ent)
    off = vec.distance_to_line(origin, direction, centre_world)
    if off > ON_AXIS_TOL:
        raise RuntimeError(
            'The sphere centre lies %.4f mm off the input axis.\n'
            'Joint A revolves about the input axis, so the axis must pass '
            'through the centre of rotation.' % (off * 10.0))
    e_a = vec.unit(direction)
    if flip_start:
        e_a = vec.negate(e_a)
    n_spine = spine_normal(e_a, up, front)
    e_f = vec.unit(vec.cross(n_spine, e_a))
    if clockwise:
        e_f = vec.negate(e_f)
    return Frame(centre_world, e_a, e_f)


def resolve(centre_world, spine_plane_ent, start_plane_ent,
            clockwise=False, flip_start=False):
    """Build the Frame, rejecting selections that cannot describe a mechanism."""
    spine = vec.plane_of(spine_plane_ent)
    start = vec.plane_of(start_plane_ent)
    n_spine = vec.unit(spine.normal)
    n_start = vec.unit(start.normal)

    off_spine = vec.distance_to_plane(spine, centre_world)
    if off_spine > 1e-6:
        raise RuntimeError(
            'The sphere centre lies %.4f mm off the spine plane.\n'
            'The spine must pass through the centre of rotation.' % (off_spine * 10.0))

    off_start = vec.distance_to_plane(start, centre_world)
    if off_start > 1e-6:
        raise RuntimeError(
            'The sphere centre lies %.4f mm off the start plane.\n'
            'The start plane must also pass through the centre of rotation, '
            'otherwise the first joint cannot sit on it.' % (off_start * 10.0))

    alignment = abs(vec.dot(n_spine, n_start))
    if alignment > 1e-6:
        raise RuntimeError(
            'The start plane must be perpendicular to the spine plane '
            '(currently %.2f deg off).\n'
            'The spine is a curve inside the spine plane, so it can only leave '
            'the start plane at a right angle if the two planes are '
            'perpendicular.' % abs(90.0 - math.degrees(math.acos(min(1.0, alignment)))))

    # A sits on the line where the planes meet...
    e_a = vec.unit(vec.cross(n_start, n_spine))
    if flip_start:
        e_a = vec.negate(e_a)

    # ...and the fan leaves along the start plane's normal, which is what makes
    # the spine perpendicular to it. cross(n_spine, e_a) reduces to +/- n_start.
    e_f = vec.unit(vec.cross(n_spine, e_a))
    if clockwise:
        e_f = vec.negate(e_f)

    return Frame(centre_world, e_a, e_f)
