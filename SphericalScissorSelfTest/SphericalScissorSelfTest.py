# Self-test for the Spherical Scissor Link Generator.
#
# Run this from Fusion's Scripts and Add-Ins dialog. It builds the skeleton in a
# throwaway document (your open designs are never touched), audits it, and
# reports. Nothing is saved; the sandbox document is closed at the end.

import adsk.core
import adsk.fusion
import traceback
import math
import os
import sys

GENERATOR_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'SphericalScissorGenerator')


def _load_generator():
    """Import the ssm package straight from the generator folder.

    Stale copies are dropped first so editing a submodule and re-running picks
    up the change without restarting Fusion.
    """
    if GENERATOR_DIR not in sys.path:
        sys.path.insert(0, GENERATOR_DIR)
    for stale in [name for name in list(sys.modules)
                  if name == 'ssm' or name.startswith('ssm.')]:
        del sys.modules[stale]
    import ssm
    from ssm import builder, parameters
    return ssm, builder, parameters


def _build_component(design, component_name):
    """The component a packed build went into, or the root for pack=False."""
    root = design.rootComponent
    for occ in root.occurrences:
        if occ.component.name == component_name:
            return occ.component
    return root


def _audit(design, comp, prefix):
    """Independent re-measurement, not a re-read of the generator's own report."""
    alpha = design.userParameters.itemByName('alpha').value
    radius = design.userParameters.itemByName('link_Radius').value
    problems = []
    checked = 0
    for sk in comp.sketches:
        if not sk.name.startswith(prefix):
            continue
        checked += 1
        if not sk.isFullyConstrained:
            problems.append('%s not fully constrained' % sk.name)
        if not sk.name.startswith(prefix + 'Link_'):
            continue
        for arc in sk.sketchCurves.sketchArcs:
            if arc.isConstruction or arc.isReference:
                continue
            ev = arc.worldGeometry.evaluator
            ok, p0, p1 = ev.getParameterExtents()
            ok, length = ev.getLengthAtParameter(p0, p1)
            want = (2 if 'Long' in sk.name else 1) * radius * alpha
            if abs(length - want) > 1e-4:
                problems.append('%s is %.4f alpha' % (sk.name, length / (radius * alpha)))
    if checked == 0:
        problems.append('audit found no %s sketches to check' % prefix)

    def check_item(item):
        try:
            if item.isGroup:
                # Group containers report Unknown health by design, and a
                # collapsed group hides its members from top-level iteration.
                for i in range(item.count):
                    check_item(item.item(i))
                return
            if item.healthState != adsk.fusion.FeatureHealthStates.HealthyFeatureHealthState:
                problems.append('unhealthy: %s' % (item.entity.name if item.entity else '?'))
        except Exception:
            pass

    for item in design.timeline:
        check_item(item)
    return problems


def run(context):
    ui = None
    doc = None
    try:
        app = adsk.core.Application.get()
        ui = app.userInterface

        if not os.path.isdir(GENERATOR_DIR):
            ui.messageBox('Generator not found at:\n%s' % GENERATOR_DIR)
            return

        ssm, builder, parameters = _load_generator()
        prefix = ssm.PREFIX
        lines = []
        failures = 0

        doc = app.documents.add(adsk.core.DocumentTypes.FusionDesignDocumentType)
        design = adsk.fusion.Design.cast(app.activeProduct)
        design.designType = adsk.fusion.DesignTypes.ParametricDesignType
        root = design.rootComponent
        centre = root.originConstructionPoint
        # The input axis (joint A's revolute axis). The origin X axis is the
        # most likely real choice and the case that used to over-constrain
        # SSM_Axis_Ref (the derived spine plane's origin is the centre, so
        # the projected axis lies on a sketch axis).
        axis = root.xConstructionAxis

        def build(**kwargs):
            return builder.build(design, centre, axis, **kwargs)

        # 1. link count and closure across rhombus counts
        lines.append('Rhombus count sweep')
        for n in (1, 2, 3, 4, 6):
            report = build(overrides={'n': str(n)}, purge=True)
            got, want = len(report['links']), 2 * n + 2
            bad = report['problems'] or (got != want)
            failures += 1 if bad else 0
            lines.append('  n=%d  links %d/%d  closure %.1e mm  %s'
                         % (n, got, want, report['closure_mm'],
                            'FAIL %s' % report['problems'] if bad else 'ok'))

        # 2. live regeneration after generation (inside the packed component)
        lines.append('')
        lines.append('Parameter regeneration (n=3)')
        build(overrides={'n': '3'}, purge=True)
        comp = _build_component(design, ssm.COMPONENT_NAME)
        table = design.userParameters
        for name, value in (('span_target', '45 deg'), ('link_Radius', '160 mm'),
                            ('beta', '9 deg'), ('span_max', '110 deg')):
            original = table.itemByName(name).expression
            table.itemByName(name).expression = value
            design.computeAll()
            problems = _audit(design, comp, prefix)
            failures += 1 if problems else 0
            lines.append('  %s = %-9s %s' % (name, value,
                                             'FAIL %s' % problems[:2] if problems else 'ok'))
            table.itemByName(name).expression = original
            design.computeAll()

        # 3. both fan directions and both start sides must build
        lines.append('')
        lines.append('Orientation options')
        for label, kwargs in (('counter-clockwise', {}),
                              ('clockwise', {'clockwise': True}),
                              ('flipped start', {'flip_start': True}),
                              ('clockwise + flipped', {'clockwise': True, 'flip_start': True})):
            report = build(overrides={'n': '3'}, purge=True, **kwargs)
            failures += 1 if report['problems'] else 0
            lines.append('  %-20s %s' % (label, 'FAIL %s' % report['problems']
                                         if report['problems'] else 'ok'))

        # 4. input axes: every origin axis (Y is vertical in a Y-up document,
        # so it exercises the front-plane fallback) must build, and a centre
        # off the axis must be refused before anything is drawn
        lines.append('')
        lines.append('Input axis')
        for label, other_axis in (('origin Y axis', root.yConstructionAxis),
                                  ('origin Z axis', root.zConstructionAxis)):
            report = builder.build(design, centre, other_axis,
                                   overrides={'n': '3'}, purge=True)
            failures += 1 if report['problems'] else 0
            lines.append('  %-16s %s' % (label, 'FAIL %s' % report['problems']
                                         if report['problems'] else 'ok'))
        probe = root.sketches.add(root.xYConstructionPlane)
        off_axis = probe.sketchPoints.add(adsk.core.Point3D.create(0, 1, 0))
        try:
            builder.build(design, off_axis, axis, overrides={'n': '3'}, purge=True)
            failures += 1
            lines.append('  centre off axis  NOT REJECTED (fail)')
        except RuntimeError as err:
            lines.append('  centre off axis  rejected: %s' % str(err).splitlines()[0][:52])
        probe.deleteMe()

        # 5. re-running must replace, not accumulate; root must stay clean
        lines.append('')
        lines.append('Idempotency & packing')
        counts = []
        for _ in range(3):
            build(overrides={'n': '3'}, purge=True)
            counts.append((root.occurrences.count, design.timeline.count,
                           root.sketches.count))
        same = len(set(counts)) == 1
        packed_clean = counts[-1][0] == 1 and counts[-1][1] == 1 and counts[-1][2] == 0
        failures += 0 if (same and packed_clean) else 1
        lines.append('  repeat builds -> (occurrences, timeline, root sketches) = %s  %s'
                     % (counts[0], 'ok' if same and packed_clean else 'FAIL %s' % counts))

        # 6. impossible parameter sets must be refused before drawing
        lines.append('')
        lines.append('Input validation')
        for label, override in (('delta/2 >= alpha', {'n': '1', 'span_target': '170 deg'}),
                                ('n = 0', {'n': '0'})):
            try:
                build(overrides=override, purge=True)
                failures += 1
                lines.append('  %-16s NOT REJECTED (fail)' % label)
            except RuntimeError as err:
                lines.append('  %-16s rejected: %s' % (label, str(err).splitlines()[0][:60]))

        # 7. solid links on top of the skeleton
        lines.append('')
        lines.append('Solid links (n=2)')
        report = build(overrides={'n': '2'}, purge=True, with_solids=True,
                           movable=False)
        comp = _build_component(design, ssm.COMPONENT_NAME)
        n_bodies = comp.bRepBodies.count
        ok_solids = (report.get('solid_bodies') == 6 and n_bodies == 6
                     and not report['problems'])
        failures += 0 if ok_solids else 1
        lines.append('  %d bodies, %d bearing joints, problems: %s  %s'
                     % (n_bodies, report.get('solid_joints', 0),
                        report['problems'] or 'none',
                        'ok' if ok_solids else 'FAIL'))

        # 8. the packed component must be pinned, and the scaffolding hidden
        lines.append('')
        lines.append('Anchoring & visibility')

        def packed_occurrence():
            for occ in root.occurrences:
                if occ.component.name == ssm.COMPONENT_NAME:
                    return occ
            return None

        def lit(collection):
            return [item.name for item in collection
                    if item.name.startswith(prefix) and item.isLightBulbOn]

        # default: grounded, spine and links visible, everything else not
        report = build(overrides={'n': '2'}, purge=True, with_solids=True,
                           movable=False)
        occ = packed_occurrence()
        grounded = bool(occ and (getattr(occ, 'isGrounded', False)
                                 or getattr(occ, 'isGroundToParent', False)))
        comp = _build_component(design, ssm.COMPONENT_NAME)
        lit_planes = lit(comp.constructionPlanes) + lit(comp.constructionAxes)
        lit_sketches = lit(comp.sketches)
        strays = [n for n in lit_sketches
                  if not (n == prefix + 'Spine' or n.startswith(prefix + 'Link_'))]
        ok_default = (grounded and report.get('ground') and not lit_planes
                      and (prefix + 'Spine') in lit_sketches and not strays)
        failures += 0 if ok_default else 1
        lines.append('  default        grounded=%s  lit planes=%d  lit sketches=%d '
                     'strays=%s  %s'
                     % (grounded, len(lit_planes), len(lit_sketches),
                        strays[:2] or 'none', 'ok' if ok_default else 'FAIL'))

        # the spine must no longer carry a reference line running past the
        # radius: SSM_Axis_OA is projected into SSM_Axis_Ref, and the spine
        # projects that bounded line instead
        radius = design.userParameters.itemByName('link_Radius').value
        spine = None
        for s in comp.sketches:
            if s.name == prefix + 'Spine':
                spine = s
        longest = 0.0
        for ln in (spine.sketchCurves.sketchLines if spine else []):
            a = ln.startSketchPoint.geometry
            b = ln.endSketchPoint.geometry
            longest = max(longest, math.hypot(b.x - a.x, b.y - a.y))
        ok_bounded = spine is not None and longest <= radius * 1.001
        failures += 0 if ok_bounded else 1
        lines.append('  spine bounded  longest line %.2f mm of %.2f mm  %s'
                     % (longest * 10, radius * 10,
                        'ok' if ok_bounded else 'FAIL'))

        # opting in hides the skeleton as well
        build(overrides={'n': '2'}, purge=True, with_solids=True,
              hide_skeleton=True, movable=False)
        comp = _build_component(design, ssm.COMPONENT_NAME)
        still_lit = lit(comp.sketches) + lit(comp.constructionPlanes)
        failures += 0 if not still_lit else 1
        lines.append('  hide_skeleton  lit %d  %s'
                     % (len(still_lit),
                        'ok' if not still_lit else 'FAIL %s' % still_lit[:3]))

        # ...and must not be sticky: the next default build shows it again
        build(overrides={'n': '2'}, purge=True, with_solids=True, ground=False,
              movable=False)
        occ = packed_occurrence()
        free = bool(occ and not getattr(occ, 'isGrounded', False))
        comp = _build_component(design, ssm.COMPONENT_NAME)
        shown = len(lit(comp.sketches))
        ok_optout = free and shown > 0
        failures += 0 if ok_optout else 1
        lines.append('  ground=False   ungrounded=%s  lit sketches=%d  %s'
                     % (free, shown, 'ok' if ok_optout else 'FAIL'))

        # 9. movable links: components + revolute joints, driven
        lines.append('')
        lines.append('Movable links (n=3)')
        report = build(overrides={'n': '3'}, purge=True, with_solids=True)
        occ = packed_occurrence()
        comp = _build_component(design, ssm.COMPONENT_NAME)
        moved = report.get('movable') or {}
        children = [o for o in comp.occurrences
                    if o.component.name.startswith(prefix + 'Link_')]
        welded = [o.name for o in children if o.isGroundToParent]
        sets = [c.name for c in design.contactSets]
        ok_struct = (not report['problems'] and moved.get('links') == 8
                     and moved.get('joints') == 11 and len(children) == 8
                     and not welded and sets.count(prefix + 'Links') == 1
                     and not lit(comp.sketches))
        failures += 0 if ok_struct else 1
        lines.append('  structure      links %s joints %s welded %s contact %s '
                     'lit sketches %d  %s'
                     % (moved.get('links'), moved.get('joints'), welded or 'none',
                        sets, len(lit(comp.sketches)),
                        'ok' if ok_struct else 'FAIL %s' % report['problems']))

        J = sys.modules['ssm.joints']
        links = {o.component.name[len(prefix + 'Link_'):]: o.createForAssemblyContext(occ)
                 for o in children}
        bore = design.userParameters.itemByName('bearing_OD').value / 2.0
        centre_pt = adsk.core.Point3D.create(0, 0, 0)
        solved = {'n': 3, 'alpha': design.userParameters.itemByName('alpha').value,
                  'span_target': design.userParameters.itemByName('span_target').value}
        joints_by_name = {j.name: j for j in comp.asBuiltJoints}

        def motion(name):
            return adsk.fusion.RevoluteJointMotion.cast(
                joints_by_name[name].createForAssemblyContext(occ).jointMotion)

        def drive(name, target, steps=30):
            m = motion(name)
            start = m.rotationValue
            for k in range(1, steps + 1):
                m.rotationValue = start + (target - start) * k / steps
            return m.rotationValue

        def closed():
            sites = J._pin_sites(links, bore, centre_pt)
            return len(sites) == 10 and all(len(s['members']) == 2 for s in sites)

        span0 = J._measured_span(links, bore, centre_pt)
        drive(prefix + 'Swivel', math.radians(20))
        swivel_drift = abs(J._measured_span(links, bore, centre_pt) - span0)
        ok_swivel = closed() and swivel_drift < math.radians(1e-3)
        drive(prefix + 'Swivel', 0.0)
        failures += 0 if ok_swivel else 1
        lines.append('  swivel 20 deg  span drift %.5f deg, closed %s  %s'
                     % (math.degrees(swivel_drift), closed(),
                        'ok' if ok_swivel else 'FAIL'))

        limits = adsk.fusion.RevoluteJointMotion.cast(
            joints_by_name[prefix + 'Span'].jointMotion).rotationLimits
        target = 0.3 * limits.minimumValue  # toward span_max: no contact there
        got = drive(prefix + 'Span', target)
        span = J._measured_span(links, bore, centre_pt)
        predicted = abs(2.0 * (J.seed_dihedral(span, solved)
                               - J.seed_dihedral(solved['span_target'], solved)))
        err = abs(predicted - abs(got))
        # the solver may settle a few hundredths of a degree short of the last
        # step; the formula check is what proves the motion is exact
        ok_span = (closed() and abs(got - target) < math.radians(0.1)
                   and err < math.radians(0.01))
        drive(prefix + 'Span', 0.0)
        failures += 0 if ok_span else 1
        lines.append('  span joint     %.2f deg -> span %.3f deg, formula error '
                     '%.5f deg, closed %s  %s'
                     % (math.degrees(got), math.degrees(span), math.degrees(err),
                        closed(), 'ok' if ok_span else 'FAIL'))

        build(overrides={'n': '3'}, purge=True, with_solids=True)
        sets = [c.name for c in design.contactSets]
        ok_rebuild = sets.count(prefix + 'Links') == 1 and root.occurrences.count == 1
        failures += 0 if ok_rebuild else 1
        lines.append('  rebuild        contact sets %s, occurrences %d  %s'
                     % (sets, root.occurrences.count, 'ok' if ok_rebuild else 'FAIL'))

        header = 'ALL CHECKS PASSED' if failures == 0 else '%d CHECK(S) FAILED' % failures
        text = header + '\n\n' + '\n'.join(lines)
        print(text)
        ui.messageBox(text, 'Spherical Scissor Generator - self test')

    except Exception:
        if ui:
            ui.messageBox('Self-test crashed:\n{}'.format(traceback.format_exc()))
    finally:
        # always discard the sandbox, even if a check threw
        if doc:
            try:
                doc.close(False)
            except Exception:
                pass
