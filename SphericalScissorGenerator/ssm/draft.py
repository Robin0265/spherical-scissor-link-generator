"""Draft skeleton preview drawn with transient custom graphics.

The dialog used to preview by really building the skeleton and letting Fusion
roll it back. On a document that already contains a generated mechanism that
meant writing the new parameter values into the document on every keystroke,
which dirtied - and recomputed - every existing feature, solids included. The
draft here touches neither the document nor the timeline: it is pure viewport
display, computed from the same closed-form solution the dialog's validation
already runs (`parameters.solve_expressions` + the frame). The real geometry
is rebuilt once, when the user presses Generate/Update.

Every link plane passes through the sphere centre, so every link arc is a
great-circle arc - which is why plain spherical interpolation between the
joint directions reproduces the skeleton exactly.
"""

import math

import adsk.core
import adsk.fusion

from . import frame as frame_mod
from . import parameters as params
from . import vectors as vec

# Segments per drawn arc - display resolution only, nothing measures these.
_STEPS = 24

_LINK_RGBA = (232, 145, 26, 255)   # orange, clearly "draft" over real geometry
_SPINE_RGBA = (128, 128, 128, 255)

# The one live graphics group. Module-level for the same reason command.py
# keeps handler references: something must own it between events.
_state = {}


def _direction(frame, phi, elevation):
    """Unit vector from the centre toward the joint at (phi, elevation)."""
    ce, se = math.cos(elevation), math.sin(elevation)
    cp, sp = math.cos(phi), math.sin(phi)
    return (ce * (cp * frame.eA.x + sp * frame.eF.x) + se * frame.eN.x,
            ce * (cp * frame.eA.y + sp * frame.eF.y) + se * frame.eN.y,
            ce * (cp * frame.eA.z + sp * frame.eF.z) + se * frame.eN.z)


def _arc_points(centre, radius, u_from, u_to):
    """Points along the great-circle arc radius*u_from -> radius*u_to."""
    d = max(-1.0, min(1.0, u_from[0] * u_to[0] + u_from[1] * u_to[1]
                      + u_from[2] * u_to[2]))
    angle = math.acos(d)
    if angle < 1e-9:
        return [(centre.x + radius * u_from[0],
                 centre.y + radius * u_from[1],
                 centre.z + radius * u_from[2])]
    s = math.sin(angle)
    points = []
    for k in range(_STEPS + 1):
        t = k / float(_STEPS)
        a = math.sin((1.0 - t) * angle) / s
        b = math.sin(t * angle) / s
        w = (a * u_from[0] + b * u_to[0],
             a * u_from[1] + b * u_to[1],
             a * u_from[2] + b * u_to[2])
        points.append((centre.x + radius * w[0],
                       centre.y + radius * w[1],
                       centre.z + radius * w[2]))
    return points


def _strips(frame, solved):
    """(spine strips, link strips), each strip a list of (x, y, z) points.

    Mirrors builder's chain: two alpha seed links from A, 2(n-1) middle links
    of 2*alpha crossing sides through a spine node, two alpha terminal links
    meeting at the apex.
    """
    R, delta, gamma, n = solved['R'], solved['delta'], solved['gamma'], solved['n']
    spine = [_arc_points(frame.C, R, _direction(frame, 0.0, 0.0),
                         _direction(frame, n * delta, 0.0))]
    links = []
    for sign in (+1.0, -1.0):
        links.append(_arc_points(frame.C, R, _direction(frame, 0.0, 0.0),
                                 _direction(frame, delta / 2.0, sign * gamma)))
        for i in range(1, n):
            strip = _arc_points(
                frame.C, R,
                _direction(frame, (i - 0.5) * delta, sign * gamma),
                _direction(frame, i * delta, 0.0))
            strip += _arc_points(
                frame.C, R,
                _direction(frame, i * delta, 0.0),
                _direction(frame, (i + 0.5) * delta, -sign * gamma))[1:]
            links.append(strip)
        links.append(_arc_points(
            frame.C, R,
            _direction(frame, (n - 0.5) * delta, sign * gamma),
            _direction(frame, n * delta, 0.0)))
    return spine, links


def draw(design, args):
    """Redraw the draft for the current dialog state."""
    clear()
    solved = params.solve_expressions(design, args['overrides'])
    frame = frame_mod.resolve(vec.point_of(args['centre_ent']),
                              args['spine_plane_ent'], args['start_plane_ent'],
                              args['clockwise'], args['flip_start'])
    spine, links = _strips(frame, solved)

    group = design.rootComponent.customGraphicsGroups.add()
    _state['group'] = group
    for strips, rgba in ((spine, _SPINE_RGBA), (links, _LINK_RGBA)):
        effect = adsk.fusion.CustomGraphicsSolidColorEffect.create(
            adsk.core.Color.create(*rgba))
        for strip in strips:
            coords = []
            for x, y, z in strip:
                coords.extend((x, y, z))
            lines = group.addLines(
                adsk.fusion.CustomGraphicsCoordinates.create(coords),
                [], True, [len(strip)])
            lines.color = effect
            try:
                lines.weight = 2
            except Exception:
                pass
    _refresh()


def clear():
    """Remove the draft. Safe to call when nothing is drawn."""
    group = _state.pop('group', None)
    if group is None:
        return
    try:
        if group.isValid:
            group.deleteMe()
            _refresh()
    except Exception:
        pass


def _refresh():
    try:
        adsk.core.Application.get().activeViewport.refresh()
    except Exception:
        pass
