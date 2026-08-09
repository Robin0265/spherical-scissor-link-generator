"""Low-level sketch primitives shared by every link.

Everything is created *nudged* off its final pose and then constrained. Creating
sketch geometry exactly on an existing point lets Fusion auto-snap it, and the
resulting hidden coincidence makes the explicit constraint added afterwards fail
with VCS_SKETCH_OVER_CONSTRAINTS.
"""

import math
import adsk.core
import adsk.fusion

from . import vectors as vec

NUDGE = 0.13
NUDGE_ANGLE = 0.06


def new_sketch(comp, plane, name):
    sketch = comp.sketches.add(plane)
    sketch.name = name
    return sketch


def project_line(sketch, line):
    projected = sketch.project(line).item(0)
    projected.isConstruction = True
    return projected


def split_spoke(projected_line, centre_world):
    """Split a projected centre->joint spoke into (centre point, joint point)."""
    start, end = projected_line.startSketchPoint, projected_line.endSketchPoint
    if vec.dist(start.worldGeometry, centre_world) < vec.dist(end.worldGeometry, centre_world):
        return start, end
    return end, start


def arc_from(sketch, centre_point, start_point, centre_world, start_world, end_world):
    """Arc welded at its centre and near end, far end left with one degree of
    freedom for the caller to remove with exactly one constraint.

    Returns (arc, far_end_point).
    """
    centre_local = sketch.modelToSketchSpace(centre_world)
    start_local = sketch.modelToSketchSpace(start_world)
    end_local = sketch.modelToSketchSpace(end_world)

    sweep = vec.signed_angle(centre_local, start_local, end_local)
    if abs(sweep) < 1e-9:
        raise RuntimeError('Degenerate link: start and end coincide.')

    radius = math.hypot(start_local.x - centre_local.x, start_local.y - centre_local.y)
    nudged_centre = vec.p(centre_local.x + NUDGE, centre_local.y + NUDGE * 0.8, 0)
    angle = math.atan2(start_local.y - centre_local.y,
                       start_local.x - centre_local.x) + math.copysign(NUDGE_ANGLE, sweep)
    nudged_start = vec.p(nudged_centre.x + radius * math.cos(angle),
                         nudged_centre.y + radius * math.sin(angle), 0)

    arc = sketch.sketchCurves.sketchArcs.addByCenterStartSweep(
        nudged_centre, nudged_start, sweep)
    constraints = sketch.geometricConstraints
    constraints.addCoincident(arc.centerSketchPoint, centre_point)

    if vec.dist(arc.startSketchPoint.geometry, nudged_start) <= \
            vec.dist(arc.endSketchPoint.geometry, nudged_start):
        near, far = arc.startSketchPoint, arc.endSketchPoint
    else:
        near, far = arc.endSketchPoint, arc.startSketchPoint
    constraints.addCoincident(near, start_point)
    return arc, far


def spoke_to(sketch, centre_point, target_point):
    """Construction radial spoke centre->target, welded at both ends.

    Downstream construction planes reference these endpoints rather than arc
    geometry, so a link arc can be rebuilt without breaking anything.
    """
    gc, gt = centre_point.geometry, target_point.geometry
    line = sketch.sketchCurves.sketchLines.addByTwoPoints(
        vec.p(gc.x + NUDGE, gc.y + NUDGE * 0.7, 0),
        vec.p(gc.x + 0.85 * (gt.x - gc.x) + NUDGE, gc.y + 0.85 * (gt.y - gc.y), 0))
    line.isConstruction = True
    constraints = sketch.geometricConstraints
    constraints.addCoincident(line.startSketchPoint, centre_point)
    constraints.addCoincident(line.endSketchPoint, target_point)
    return line


def chord(sketch, point_a, point_b):
    ga, gb = point_a.geometry, point_b.geometry
    line = sketch.sketchCurves.sketchLines.addByTwoPoints(
        vec.p(ga.x + NUDGE, ga.y + NUDGE, 0),
        vec.p(gb.x + NUDGE, gb.y + NUDGE, 0))
    line.isConstruction = True
    constraints = sketch.geometricConstraints
    constraints.addCoincident(line.startSketchPoint, point_a)
    constraints.addCoincident(line.endSketchPoint, point_b)
    return line


def point_on_curve_spoke(sketch, centre_point, curve, target_world):
    """Spoke from the centre whose far end rides on `curve`."""
    centre_local = centre_point.geometry
    target_local = sketch.modelToSketchSpace(target_world)
    line = sketch.sketchCurves.sketchLines.addByTwoPoints(
        vec.p(centre_local.x + NUDGE, centre_local.y + NUDGE * 0.7, 0),
        vec.p(target_local.x + NUDGE, target_local.y + NUDGE, 0))
    line.isConstruction = True
    constraints = sketch.geometricConstraints
    constraints.addCoincident(line.startSketchPoint, centre_point)
    constraints.addCoincident(line.endSketchPoint, curve)
    return line


def angular_dim(sketch, line_one, line_two, centre_local, expression):
    """Angular dimension whose text sits on the bisector.

    The text position is what tells Fusion which of the four quadrants to
    dimension, so it has to land between the two spokes.
    """
    def far_end(line):
        start, end = line.startSketchPoint.geometry, line.endSketchPoint.geometry
        return end if vec.dist(start, centre_local) < vec.dist(end, centre_local) else start

    e1, e2 = far_end(line_one), far_end(line_two)
    a1 = math.atan2(e1.y - centre_local.y, e1.x - centre_local.x)
    a2 = math.atan2(e2.y - centre_local.y, e2.x - centre_local.x)
    delta = a2 - a1
    while delta > math.pi:
        delta -= 2 * math.pi
    while delta < -math.pi:
        delta += 2 * math.pi

    mid = a1 + delta / 2.0
    radius = 0.45 * max(vec.dist(e1, centre_local), vec.dist(e2, centre_local))
    text = vec.p(centre_local.x + radius * math.cos(mid),
                 centre_local.y + radius * math.sin(mid), 0)

    dimension = sketch.sketchDimensions.addAngularDimension(line_one, line_two, text)
    dimension.parameter.expression = expression
    return dimension


def radial_dim(sketch, arc, centre_local, radius, expression):
    text = vec.p(centre_local.x + 0.6 * radius, centre_local.y - 0.6 * radius, 0)
    dimension = sketch.sketchDimensions.addRadialDimension(arc, text)
    dimension.parameter.expression = expression
    return dimension
