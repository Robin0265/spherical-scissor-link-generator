"""Resolving the sphere frame from the user's selections.

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
