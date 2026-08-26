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
from . import progress
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


AXIS_REF_NAME = PREFIX + 'Axis_Ref'


def build_axis_reference(comp, plane_ent, centre_ent, axis, frame, solved):
    """A link_Radius-long stand-in for SSM_Axis_OA, in a sketch of its own.

    Projecting a *construction axis* into a sketch gives a reference line whose
    length Fusion picks, and it ran well past link_Radius on both sides of the
    centre - the one piece of the skeleton nobody wants to look at. Projecting
    a *sketch line* brings it across at its own length instead. So the axis is
    projected exactly once, here, in a sketch that stays hidden, and a
    radius-long line is constrained along it; the spine projects that.

    The live-driver chain is unchanged - line -> projected axis -> the two
    planes the user picked - so moving the start plane still re-orients the
    whole fan.
    """
    sketch = sk.new_sketch(comp, plane_ent, AXIS_REF_NAME)
    centre_point = sketch.project(centre_ent).item(0)
    axis_ref = sketch.project(axis).item(0)
    axis_ref.isConstruction = True

    centre_local = sketch.modelToSketchSpace(frame.C)
    a_local = sketch.modelToSketchSpace(frame.joint(solved['R'], 0.0, 0.0))

    sketch.isComputeDeferred = True
    try:
        line = sketch.sketchCurves.sketchLines.addByTwoPoints(
            vec.p(centre_local.x + sk.NUDGE, centre_local.y + sk.NUDGE * 0.7, 0),
            vec.p(a_local.x + sk.NUDGE, a_local.y + sk.NUDGE * 0.9, 0))
        line.isConstruction = True
        constraints = sketch.geometricConstraints
        constraints.addCoincident(line.startSketchPoint, centre_point)
        # Point-on-line rather than collinear: the far end still needs a
        # length, and "on the axis + link_Radius from the centre" has two
        # solutions, so the nudge above is what puts the solver on the right
        # one - the same technique the link arcs use.
        constraints.addCoincident(line.endSketchPoint, axis_ref)
        dimension = sketch.sketchDimensions.addDistanceDimension(
            line.startSketchPoint, line.endSketchPoint,
            adsk.fusion.DimensionOrientations.AlignedDimensionOrientation,
            vec.p((centre_local.x + a_local.x) / 2 + 0.4,
                  (centre_local.y + a_local.y) / 2 + 0.4, 0))
        dimension.parameter.expression = 'link_Radius'
    finally:
        sketch.isComputeDeferred = False

    # "On the axis, link_Radius from the centre" has two solutions, one at
    # each end of OA. The nudge should land on joint A's, but a silent flip
    # here would build a correctly-formed mechanism pointing the wrong way -
    # closure and link lengths would all still check out - so it is asserted
    # rather than trusted.
    landed = line.endSketchPoint.worldGeometry
    want = frame.joint(solved['R'], 0.0, 0.0)
    off = vec.dist(landed, want)
    if off > 1e-4:
        raise RuntimeError(
            'The axis reference solved onto the wrong end of OA (%.4f mm from '
            'joint A). Try "Start from the opposite side".' % (off * 10.0))
    return sketch, line


def build_spine(comp, plane_ent, centre_ent, axis_line, frame, solved):
    """Spine sketch: the span_target arc plus the 2n+1 radial fan.

    Only one angular dimension is needed; the equal-chord chain propagates the
    delta/2 spacing to every node, and the arc's sweep follows from it.

    The fan's absolute orientation comes from `axis_line`, the bounded stand-in
    build_axis_reference put along the plane intersection. Projecting it in and
    holding the first spoke collinear with it keeps the start plane a live
    driver: move that plane and the whole chain re-orients.
    """
    sketch = sk.new_sketch(comp, plane_ent, PREFIX + 'Spine')
    constraints = sketch.geometricConstraints

    centre_point = sketch.project(centre_ent).item(0)
    projected_axis = sketch.project(axis_line).item(0)
    projected_axis.isConstruction = True
    radius, delta, n = solved['R'], solved['delta'], solved['n']
    nodes = [frame.joint(radius, k * delta / 2.0, 0.0) for k in range(2 * n + 1)]

    centre_local = sketch.modelToSketchSpace(frame.C)
    a_local = sketch.modelToSketchSpace(nodes[0])
    j_local = sketch.modelToSketchSpace(nodes[-1])
    sweep = vec.signed_angle(centre_local, a_local, j_local)
    radius_local = math.hypot(a_local.x - centre_local.x, a_local.y - centre_local.y)

    # One deferred solve at the end instead of one per constraint. Everything
    # is created nudged just off its final pose, so the single solve starts
    # from the same near-solution guess the incremental solves did and lands
    # on the same branch.
    sketch.isComputeDeferred = True
    try:
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
    finally:
        sketch.isComputeDeferred = False
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

    sketch.isComputeDeferred = True
    try:
        _arc, far = sk.arc_from(sketch, centre_point, a_point, frame.C,
                                frame.joint(radius, 0.0, 0.0), pin_world)
        spoke = sk.spoke_to(sketch, centre_point, far)
        sk.angular_dim(sketch, projected_a, spoke, centre_point.geometry, 'alpha')
    finally:
        sketch.isComputeDeferred = False
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

    sketch.isComputeDeferred = True
    try:
        _arc, far = sk.arc_from(sketch, centre_point, pin_point, frame.C,
                                start_world, end_world)
        chord_in = sk.chord(sketch, pin_point, node_projected)
        chord_out = sk.chord(sketch, node_projected, far)
        sketch.geometricConstraints.addEqual(chord_out, chord_in)
        spoke = sk.spoke_to(sketch, centre_point, far)
    finally:
        sketch.isComputeDeferred = False
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

    sketch.isComputeDeferred = True
    try:
        _arc, far = sk.arc_from(sketch, centre_point, pin_point, frame.C,
                                start_world, end_world)
        spoke = sk.spoke_to(sketch, centre_point, far)
        sk.angular_dim(sketch, projected_previous, spoke, centre_point.geometry, 'alpha')
    finally:
        sketch.isComputeDeferred = False
    return sketch, spoke, far


def _set_bulb(item, on):
    """Light bulb on/off, tolerating entities that refuse it. True if changed."""
    try:
        if item.isLightBulbOn == on:
            return False
        item.isLightBulbOn = on
        return True
    except Exception:
        return False


def _is_skeleton_sketch(name):
    """The two sketch families worth looking at: the spine and the links.

    Everything else the run creates - the axis reference, and the solid stage's
    profile and boss sketches - is scaffolding that is always hidden, whatever
    the skeleton option says. Turning those back on would bury the model in
    rectangles and bore circles.
    """
    return (name == PREFIX + 'Spine'
            or name.startswith(PREFIX + 'Link_'))


def hide_scaffolding(comp, hide_sketches):
    """Turn the light bulbs off on everything this run created as scaffolding.

    Construction planes, axes and points always go: they only exist to carry
    the sketches, and the 2n+2 link planes are pure clutter once the links are
    drawn on them. So do the non-skeleton sketches (see _is_skeleton_sketch).

    Only the spine and link sketches follow `hide_sketches`, and they are now
    safe to leave visible: the over-long projected SSM_Axis_OA line that used
    to make the spine unreadable lives in SSM_Axis_Ref instead, which is never
    shown (see build_axis_reference).

    Nothing is deleted - ticking the light bulbs back on in the browser brings
    any of it back.
    """
    hidden = 0
    for collection in (comp.constructionPlanes, comp.constructionAxes,
                       comp.constructionPoints):
        for item in collection:
            if item.name.startswith(PREFIX) and _set_bulb(item, False):
                hidden += 1
    for sketch in comp.sketches:
        if not sketch.name.startswith(PREFIX):
            continue
        show = _is_skeleton_sketch(sketch.name) and not hide_sketches
        if _set_bulb(sketch, show) and not show:
            hidden += 1
    return hidden


def ground_occurrence(occurrence):
    """Pin the packed component so it cannot be dragged off its anchor.

    Every sketch inside the mechanism is tied to the centre point and the two
    planes the user selected, but the occurrence that holds them carries its
    own transform, and that transform is a free degree of freedom: dragging the
    component in the canvas moves the bodies while the skeleton stays behind.
    Grounding is what removes it.

    Returns True only if the occurrence reports itself grounded afterwards, so
    a property that silently refuses the write is never mistaken for success.
    """
    for attribute in ('isGrounded', 'isGroundToParent'):
        try:
            setattr(occurrence, attribute, True)
            if getattr(occurrence, attribute):
                return True
        except Exception:
            continue
    return False


def build(design, centre_ent, spine_plane_ent, start_plane_ent, overrides=None,
          clockwise=False, flip_start=False, purge=True, pack=True,
          with_solids=False, with_audit=True, reporter=None,
          ground=True, hide_skeleton=False):
    """Generate the whole skeleton. Returns an audit report.

    with_audit=False skips the final full-timeline recompute and the audit's
    sketch-by-sketch re-measurement, for callers that never show the report.

    `reporter` is an optional progress.Reporter. When present the build shows
    a progress dialog and pumps the UI event loop between features, so Fusion
    stays painted and the user can cancel; cancelling raises
    progress.Cancelled (a RuntimeError with a readable message). The caller
    owns reporter.end().

    With pack=True (the default) everything is built inside a sub-component
    named COMPONENT_NAME and the timeline range is collapsed into one named
    group, so the run appears as a single object in both the browser and the
    timeline.

    with_solids=True additionally builds the physical link bodies (bars,
    bosses, bores) via the solids module. A solids failure is reported in the
    returned audit rather than raised, so a correct skeleton is never lost.

    ground=True (the default) grounds the packed occurrence, so the mechanism
    cannot be dragged away from the centre it was built about. Ungrounded it
    only looks anchored: the sketches are tied to the user's selections while
    the occurrence transform above them stays free.

    hide_skeleton=True additionally hides the spine and link sketches. It
    defaults to False: construction planes, the axis reference and the solid
    stage's own sketches are hidden regardless, which leaves the spine arc and
    its fan - which are worth seeing - and nothing else.

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

    if reporter is not None:
        # purge + parameter write + skeleton (spine, 2 seeds, 2(n-1) longs,
        # 2 ends) + audit + solid stage (2n+2 bars, 6n+2 bosses, audit).
        n_planned = preview['n']
        reporter.begin((1 if purge else 0) + 1 + (2 * n_planned + 3)
                       + (1 if with_audit else 0)
                       + ((8 * n_planned + 5) if with_solids else 0))

    if purge:
        purge_previous(design)
        progress.tick(reporter, 'Previous run removed')
    # after the old geometry is gone, stale parameters from earlier
    # conventions can be dropped (no-op when still referenced)
    params.remove_legacy(design)

    # Parameters are written only after the purge: writing them first dirtied
    # the old mechanism - solids included - and forced a full recompute of
    # geometry that was about to be deleted anyway.
    params.ensure_parameters(design, overrides)
    design.computeAll()
    solved = params.read_solved(design)
    params.validate(solved)
    progress.tick(reporter, 'Parameters written')

    # Everything from here down is this run's own timeline range; the audit's
    # health check is scoped to it so a large host document's timeline is
    # never re-scanned.
    timeline_start = design.timeline.count

    pack_note = None
    comp = root
    group_start = None
    occurrence = None
    grounded = False
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
            # Grounding writes a timeline node of its own, so it has to happen
            # here rather than at the end of the build: after the group (or the
            # custom feature) has claimed this run's range, that node lands
            # outside it and a packed build stops being a single timeline
            # entry. Created now, it sits at group_start + 1 and is absorbed.
            if ground:
                grounded = ground_occurrence(occurrence)

    # Everything from here belongs to `comp`; the occurrence node and the
    # ground node above it belong to the root. custom_feature.wrap needs the
    # difference when it tries to hang the feature off the sub-component.
    component_start = design.timeline.count

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

    # No computeAll between stages: each sketch is solved when its deferred
    # compute is released, and features (planes, axes) evaluate on creation.
    # A full-timeline recompute here would re-solve everything built so far
    # at every stage, which is quadratic in n.
    _axis_sketch, axis_line = build_axis_reference(
        comp, spine_plane_ent, centre_ent, axis, frame, solved)
    spine_sketch, spokes, node_points = build_spine(
        comp, spine_plane_ent, centre_ent, axis_line, frame, solved)
    progress.tick(reporter, 'Spine sketch')

    _sk_p, spoke_p, pin_p, _w = build_seed(
        comp, spokes[0], spine_plane_ent, axis, frame, solved, +1, 'Seed_P')
    progress.tick(reporter, 'Link Seed_P')
    _sk_n, spoke_n, pin_n, _w = build_seed(
        comp, spokes[0], spine_plane_ent, axis, frame, solved, -1, 'Seed_N')
    progress.tick(reporter, 'Link Seed_N')

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
        progress.tick(reporter, 'Link Long%d_P' % i)
        _sk_b, spoke_b, far_b = build_long(
            comp, centre_ent, current[-1][0], current[-1][1], node_spoke, node_point,
            frame, current[-1][2], next_p, 'Long%d_N' % i)
        progress.tick(reporter, 'Link Long%d_N' % i)
        current = {+1: (spoke_b, far_b, next_p), -1: (spoke_a, far_a, next_n)}

    apex = frame.joint(radius, n * delta, 0.0)
    for sign, tag in ((+1, 'End_P'), (-1, 'End_N')):
        build_end(comp, centre_ent, current[sign][0], current[sign][1],
                  spokes[2 * n], node_points[2 * n], frame,
                  current[sign][2], apex, tag)
        progress.tick(reporter, 'Link ' + tag)

    if with_audit:
        # The one full recompute of the run, so the audit measures settled
        # geometry rather than anything still marked dirty.
        design.computeAll()
        report = audit.verify(design, comp, solved, frame,
                              timeline_from=timeline_start)
        progress.tick(reporter, 'Skeleton audited')
    else:
        report = {'links': [], 'problems': [], 'n': solved['n'],
                  'closure_mm': 0.0}

    if with_solids:
        try:
            solid_report = solids.build_all(design, comp, frame.C, reporter)
            report['solid_bodies'] = solid_report['bodies']
            report['solid_joints'] = solid_report['joints']
            report['problems'].extend(solid_report['problems'])
        except progress.Cancelled:
            # Cancel means stop, not "keep going and note a problem".
            raise
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
                     'solids': with_solids, 'ground': ground,
                     'hide_skeleton': hide_skeleton},
            component_start=component_start)
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

    # Visibility is display state, not a feature: it writes nothing to the
    # timeline, so unlike the grounding above it can safely happen last.
    hidden = hide_scaffolding(comp, hide_skeleton)
    if hidden:
        report['hidden'] = hidden
        report['hid_sketches'] = hide_skeleton

    if occurrence is not None and ground:
        if grounded:
            report['ground'] = True
        else:
            report['problems'].append(
                'could not ground the component - right-click "%s" in the '
                'browser and choose Ground, or it can be dragged off its '
                'anchor.' % COMPONENT_NAME)

    if pack_note:
        report['pack_note'] = pack_note

    return report
