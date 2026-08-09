"""Small 3D helpers. Nothing here touches the document."""

import math
import adsk.core


def v(x, y, z):
    return adsk.core.Vector3D.create(x, y, z)


def p(x, y, z):
    return adsk.core.Point3D.create(x, y, z)


def unit(vec):
    n = vec.length
    if n < 1e-12:
        raise RuntimeError('Cannot normalise a zero-length vector.')
    return v(vec.x / n, vec.y / n, vec.z / n)


def negate(vec):
    return v(-vec.x, -vec.y, -vec.z)


def cross(a, b):
    return v(a.y * b.z - a.z * b.y,
             a.z * b.x - a.x * b.z,
             a.x * b.y - a.y * b.x)


def dot(a, b):
    return a.x * b.x + a.y * b.y + a.z * b.z


def dist(a, b):
    return math.sqrt((a.x - b.x) ** 2 + (a.y - b.y) ** 2 + (a.z - b.z) ** 2)


def plane_of(entity):
    """adsk.core.Plane for a construction plane or a planar face."""
    plane = adsk.core.Plane.cast(entity.geometry)
    if plane is None:
        raise RuntimeError('The selected face is not planar.')
    return plane


def point_of(entity):
    """World Point3D of a point-like entity (sketch point, vertex, work point)."""
    for attr in ('worldGeometry', 'geometry', 'point'):
        if hasattr(entity, attr):
            pt = adsk.core.Point3D.cast(getattr(entity, attr))
            if pt:
                return pt
    raise RuntimeError('The selected entity does not resolve to a point.')


def distance_to_plane(plane, point):
    """Perpendicular distance from a world point to an adsk.core.Plane."""
    n, o = plane.normal, plane.origin
    return abs(n.x * (point.x - o.x) + n.y * (point.y - o.y) + n.z * (point.z - o.z))


def signed_angle(centre, start, end):
    """Signed sweep (radians, shortest way) from start to end about centre.

    All three are sketch-space Point3D; only x and y are used.
    """
    a0 = math.atan2(start.y - centre.y, start.x - centre.x)
    a1 = math.atan2(end.y - centre.y, end.x - centre.x)
    d = a1 - a0
    while d > math.pi:
        d -= 2 * math.pi
    while d < -math.pi:
        d += 2 * math.pi
    return d
