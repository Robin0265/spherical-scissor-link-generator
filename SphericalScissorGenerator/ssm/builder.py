"""The link chain.

For n rhombi the mechanism is 2n+2 links: two alpha seed links from A, then
2(n-1) middle links of 2*alpha that cross from one side to the other through a
spine node, then two alpha terminal links meeting at the apex.

A link's role depends on n, so the whole chain is generated in one pass. Growing
an existing drawing would mean turning a terminal link into a middle link, which
in a history-based modeller either splits one link into two curves or forces
destructive edits of committed features.
"""

import math
import adsk.core
import adsk.fusion

from . import vectors as vec
from . import parameters as params
from . import sketching as sk
from . import frame as frame_mod
from . import audit
from . import custom_feature
from . import solids
from . import PREFIX, COMPONENT_NAME


def purge_previous(design):
    """Delete everything a previous run created.

    Removes packed runs (sub-components named COMPONENT_NAME) and, for
    compatibility with earlier versions, loose PREFIX-named features in the
    root component. Nothing else is touched.
    """
    root = design.rootComponent
    removed = 0
    for occ in list(root.occurrences):
        try:
            if occ.component.name.startswith(COMPONENT_NAME):
                if occ.deleteMe():
                    removed += 1
        except Exception:
            pass
    # Loose (pack=False) solid builds live in the root: remove their features
    # first so the swept/extruded bodies go with them.
    for collection in (root.features.extrudeFeatures, root.features.sweepFeatures):
        for feature in list(collection):
            name = ''
            try:
                profile = feature.profile
                if hasattr(profile, 'parentSketch'):
                    name = profile.parentSketch.name
            except Exception:
                pass
            if name.startswith(PREFIX):
                try:
                    if feature.deleteMe():
                        removed += 1
                except Exception:
                    pass
    for body in list(root.bRepBodies):
        if body.name.startswith(PREFIX):
            try:
                if body.deleteMe():
                    removed += 1
            except Exception:
                pass
    for collection in (root.sketches, root.constructionPlanes, root.constructionAxes):
        for item in list(collection):
            if item.name.startswith(PREFIX):
                try:
                    if item.deleteMe():
                        removed += 1
                except Exception:
                    pass
    return removed


def build_spine(comp, plane_ent, centre_ent, axis, frame, solved):
    """Spine sketch: the span_target arc plus the 2n+1 radial fan.

    Only one angular dimension is needed; the equal-chord chain propagates the
    delta/2 spacing to every node, and the arc's sweep follows from it.

    The fan's absolute orientation comes from `axis`, the line where the start
    plane meets the spine plane. Projecting it in and holding the first spoke
    collinear with it keeps the start plane a live driver: move that plane and
    the whole chain re-orients.
    """
    sketch = sk.new_sketch(comp, plane_ent, PREFIX + 'Spine')
    constraints = sketch.geometricConstraints

    centre_point = sketch.project(centre_ent).item(0)
    projected_axis = sketch.project(axis).item(0)
    projected_axis.isConstruction = True
    radius, delta, n = solved['R'], solved['delta'], solved['n']
    nodes = [frame.joint(radius, k * delta / 2.0, 0.0) for k in range(2 * n + 1)]

    centre_local = sketch.modelToSketchSpace(frame.C)
    a_local = sketch.modelToSketchSpace(nodes[0])
    j_local = sketch.modelToSketchSpace(nodes[-1])
    sweep = vec.signed_angle(centre_local, a_local, j_local)
    radius_local = math.hypot(a_local.x - centre_local.x, a_local.y - centre_local.y)

    nudged_centre = vec.p(centre_local.x + sk.NUDGE, centre_local.y + sk.NUDGE * 0.8, 0)
    angle = math.atan2(a_local.y - centre_local.y,
                       a_local.x - centre_local.x) + math.copysign(sk.NUDGE_ANGLE, sweep)
    nudged_a = vec.p(nudged_centre.x + radius_local * math.cos(angle),
                     nudged_centre.y + radius_local * math.sin(angle), 0)
    arc = sketch.sketchCurves.sketchArcs.addByCenterStartSweep(
        nudged_centre, nudged_a, sweep)

    if vec.dist(arc.startSketchPoint.geometry, nudged_a) <= \
            vec.dist(arc.endSketchPoint.geometry, nudged_a):
        a_point, j_point = arc.startSketchPoint, arc.endSketchPoint
    else:
        a_point, j_point = arc.endSketchPoint, arc.startSketchPoint

    constraints.addCoincident(arc.centerSketchPoint, centre_point)
    sk.radial_dim(sketch, arc, centre_local, radius_local, 'link_Radius')

    spokes, node_points = [], []
    for k in range(2 * n + 1):
        if k == 0:
            spoke_a = sk.spoke_to(sketch, centre_point, a_point)
            # A lies on the plane intersection, which is what anchors the fan.
            constraints.addCollinear(spoke_a, projected_axis)
            spokes.append(spoke_a)
            node_points.append(a_point)
        elif k == 2 * n:
            spokes.append(sk.spoke_to(sketch, centre_point, j_point))
            node_points.append(j_point)
        else:
            line = sk.point_on_curve_spoke(sketch, centre_point, arc, nodes[k])
            spokes.append(line)
            node_points.append(line.endSketchPoint)

    chords = [sk.chord(sketch, node_points[k], node_points[k + 1]) for k in range(2 * n)]
    for k in range(1, len(chords)):
        constraints.addEqual(chords[k], chords[0])

    sk.angular_dim(sketch, spokes[0], spokes[1], centre_local, 'delta / 2')
    return sketch, spokes, node_points


def build_seed(comp, spine_spoke_a, plane_ent, axis, frame, solved, sign, name):
    """One of the two mirror-image alpha links A -> P1, on a +/-lambda plane."""
    radius, delta, gamma = solved['R'], solved['delta'], solved['gamma']
    pin_world = frame.joint(radius, delta / 2.0, sign * gamma)

    plane = None
    for expression in ('lambda', '-lambda'):
        candidate_input = comp.constructionPlanes.createInput()
        candidate_input.setByAngle(
            axis, adsk.core.ValueInput.createByString(expression), plane_ent)
        candidate = comp.constructionPlanes.add(candidate_input)
        if vec.distance_to_plane(candidate.geometry, pin_world) < 1e-4:
            plane = candidate
            break
        candidate.deleteMe()
    if plane is None:
        raise RuntimeError('Could not orient the seed plane for the %s pin.' % name)
    plane.name = PREFIX + 'Plane_' + name

    sketch = sk.new_sketch(comp, plane, PREFIX + 'Link_' + name)
    projected_a = sk.project_line(sketch, spine_spoke_a)
    centre_point, a_point = sk.split_spoke(projected_a, frame.C)

    _arc, far = sk.arc_from(sketch, centre_point, a_point, frame.C,
                            frame.joint(radius, 0.0, 0.0), pin_world)
    spoke = sk.spoke_to(sketch, centre_point, far)
    sk.angular_dim(sketch, projected_a, spoke, centre_point.geometry, 'alpha')
    return sketch, spoke, far, pin_world


def build_long(comp, centre_ent, previous_spoke, previous_pin_point,
               node_spoke, node_point, frame, start_world, end_world, name):
    """A 2*alpha middle link: previous pin -> spine node -> next pin.

    The far pin is placed by an equal-chord constraint rather than a dimension,
    so the link measuring exactly 2*alpha is a consequence of closure and stays
    available as a check.
    """
    plane_input = comp.constructionPlanes.createInput()
    plane_input.setByThreePoints(centre_ent, previous_pin_point, node_point)
    plane = comp.constructionPlanes.add(plane_input)
    plane.name = PREFIX + 'Plane_' + name

    sketch = sk.new_sketch(comp, plane, PREFIX + 'Link_' + name)
    projected_previous = sk.project_line(sketch, previous_spoke)
    projected_node = sk.project_line(sketch, node_spoke)
    centre_point, pin_point = sk.split_spoke(projected_previous, frame.C)
    _centre, node_projected = sk.split_spoke(projected_node, frame.C)

    _arc, far = sk.arc_from(sketch, centre_point, pin_point, frame.C,
                            start_world, end_world)
    chord_in = sk.chord(sketch, pin_point, node_projected)
    chord_out = sk.chord(sketch, node_projected, far)
    sketch.geometricConstraints.addEqual(chord_out, chord_in)
    spoke = sk.spoke_to(sketch, centre_point, far)
    return sketch, spoke, far


def build_end(comp, centre_ent, previous_spoke, previous_pin_point,
              node_spoke, node_point, frame, start_world, end_world, name):
    """A terminal alpha link.

    The apex end is dimensioned to alpha and deliberately not welded to the
    spine, so where it lands is an independent check on the formulation.
    """
    plane_input = comp.constructionPlanes.createInput()
    plane_input.setByThreePoints(centre_ent, previous_pin_point, node_point)
    plane = comp.constructionPlanes.add(plane_input)
    plane.name = PREFIX + 'Plane_' + name

    sketch = sk.new_sketch(comp, plane, PREFIX + 'Link_' + name)
    projected_previous = sk.project_line(sketch, previous_spoke)
    sk.project_line(sketch, node_spoke)
    centre_point, pin_point = sk.split_spoke(projected_previous, frame.C)

    _arc, far = sk.arc_from(sketch, centre_point, pin_point, frame.C,
                            start_world, end_world)
    spoke = sk.spoke_to(sketch, centre_point, far)
    sk.angular_dim(sketch, projected_previous, spoke, centre_point.geometry, 'alpha')
    return sketch, spoke, far


def build(design, centre_ent, spine_plane_ent, start_plane_ent, overrides=None,
          clockwise=False, flip_start=False, purge=True, pack=True,
          with_solids=False):
    """Generate the whole skeleton. Returns an audit report.

    With pack=True (the default) everything is built inside a sub-component
    named COMPONENT_NAME and the timeline range is collapsed into one named
    group, so the run appears as a single object in both the browser and the
    timeline.

    with_solids=True additionally builds the physical link bodies (bars,
    bosses, bores) via the solids module. A solids failure is reported in the
    returned audit rather than raised, so a correct skeleton is never lost.

    Raises RuntimeError with a readable reason for any unusable input; the
    document is left untouched when that happens, because everything is checked
    before the first feature is created.
    """
    root = design.rootComponent

    # Solve and check before touching the document.
    preview = params.solve_expressions(design, overrides)
    centre_world = vec.point_of(centre_ent)
    frame = frame_mod.resolve(centre_world, spine_plane_ent, start_plane_ent,
                              clockwise, flip_start)

    params.ensure_parameters(design, overrides)
    design.computeAll()
    solved = params.read_solved(design)
    params.validate(solved)

    if purge:
        purge_previous(design)
        design.computeAll()
    # after the old geometry is gone, stale parameters from earlier
    # conventions can be dropped (no-op when still referenced)
    params.remove_legacy(design)

    pack_note = None
    comp = root
    group_start = None
    if pack:
        # Part Design documents (new in 2026) allow exactly one component, so
        # packing into a sub-component is impossible there. Fall back to
        # building loose in the root; the timeline group is still applied.
        try:
            occurrence = root.occurrences.addNewComponent(adsk.core.Matrix3D.create())
        except RuntimeError as err:
            if 'one component' not in str(err) and 'Part Design' not in str(err):
                raise
            pack = False
            group_start = design.timeline.count
            pack_note = ('This is a Part Design document (single component only), '
                         'so the mechanism was built into the root component. '
                         'The timeline is still collapsed into one group. Use a '
                         'Hybrid Design or Assembly document to get the packed '
                         'sub-component.')
        else:
            comp = occurrence.component
            comp.name = COMPONENT_NAME
            group_start = occurrence.timelineObject.index

    radius, delta, gamma, n = solved['R'], solved['delta'], solved['gamma'], solved['n']

    # The line where the two planes meet is both the direction of joint A and
    # the hinge the seed link planes are rotated about, so it is made once here.
    axis_input = comp.constructionAxes.createInput()
    axis_input.setByTwoPlanes(spine_plane_ent, start_plane_ent)
    axis = comp.constructionAxes.add(axis_input)
    axis.name = PREFIX + 'Axis_OA'
    # Scaffolding, like the link planes: keep it out of the viewport. Hiding
    # does not affect the seed planes or the spine projection that reference it.
    try:
        axis.isLightBulbOn = False
    except Exception:
        pass

    spine_sketch, spokes, node_points = build_spine(
        comp, spine_plane_ent, centre_ent, axis, frame, solved)
    design.computeAll()

    _sk_p, spoke_p, pin_p, _w = build_seed(
        comp, spokes[0], spine_plane_ent, axis, frame, solved, +1, 'Seed_P')
    _sk_n, spoke_n, pin_n, _w = build_seed(
        comp, spokes[0], spine_plane_ent, axis, frame, solved, -1, 'Seed_N')
    design.computeAll()

    current = {
        +1: (spoke_p, pin_p, frame.joint(radius, delta / 2.0, +gamma)),
        -1: (spoke_n, pin_n, frame.joint(radius, delta / 2.0, -gamma)),
    }

    for i in range(1, n):
        node_world = frame.joint(radius, i * delta, 0.0)
        node_spoke, node_point = spokes[2 * i], node_points[2 * i]
        next_p = frame.joint(radius, (i + 0.5) * delta, +gamma)
        next_n = frame.joint(radius, (i + 0.5) * delta, -gamma)

        # the + side link crosses over and lands on the - side, and vice versa
        _sk_a, spoke_a, far_a = build_long(
            comp, centre_ent, current[+1][0], current[+1][1], node_spoke, node_point,
            frame, current[+1][2], next_n, 'Long%d_P' % i)
        _sk_b, spoke_b, far_b = build_long(
            comp, centre_ent, current[-1][0], current[-1][1], node_spoke, node_point,
            frame, current[-1][2], next_p, 'Long%d_N' % i)
        design.computeAll()
        current = {+1: (spoke_b, far_b, next_p), -1: (spoke_a, far_a, next_n)}

    apex = frame.joint(radius, n * delta, 0.0)
    for sign, tag in ((+1, 'End_P'), (-1, 'End_N')):
        build_end(comp, centre_ent, current[sign][0], current[sign][1],
                  spokes[2 * n], node_points[2 * n], frame,
                  current[sign][2], apex, tag)
    design.computeAll()

    report = audit.verify(design, comp, solved, frame)

    if with_solids:
        try:
            solid_report = solids.build_all(design, comp, frame.C)
            report['solid_bodies'] = solid_report['bodies']
            report['solid_joints'] = solid_report['joints']
            report['problems'].extend(solid_report['problems'])
        except Exception as err:
            report['problems'].append('solid stage failed: %s'
                                      % str(err).splitlines()[-1])
    if pack:
        report['component'] = comp.name

    if group_start is not None and design.timeline.count > group_start:
        # Prefer a real custom feature: one editable timeline node instead of a
        # folder. It is only available from the add-in, and Fusion may still
        # refuse it, so the timeline group remains the fallback. The two are
        # alternatives - both would try to own the same timeline range.
        feature = custom_feature.wrap(
            design, root, comp, group_start, solved, overrides,
            selections={'centrePoint': centre_ent,
                        'spinePlane': spine_plane_ent,
                        'startPlane': start_plane_ent},
            options={'clockwise': clockwise, 'flip_start': flip_start,
                     'solids': with_solids})
        if feature is not None:
            report['custom_feature'] = feature.name
            report['grouped'] = True
        else:
            design.timeline.moveToEnd()
            group = design.timeline.timelineGroups.add(
                group_start, design.timeline.count - 1)
            group.name = COMPONENT_NAME
            group.isCollapsed = True
            report['grouped'] = True

    if pack_note:
        report['pack_note'] = pack_note

    return report
