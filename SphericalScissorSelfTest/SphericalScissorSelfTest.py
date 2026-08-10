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
        spine = root.xZConstructionPlane
        start = root.xYConstructionPlane

        def build(**kwargs):
            return builder.build(design, centre, spine, start, **kwargs)

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

        # 4. the start plane must be perpendicular to the spine plane
        lines.append('')
        lines.append('Plane relationship')
        try:
            builder.build(design, centre, spine, spine, overrides={'n': '3'}, purge=True)
            failures += 1
            lines.append('  parallel planes  NOT REJECTED (fail)')
        except RuntimeError as err:
            lines.append('  parallel planes  rejected: %s' % str(err).splitlines()[0][:52])

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
        report = build(overrides={'n': '2'}, purge=True, with_solids=True)
        comp = _build_component(design, ssm.COMPONENT_NAME)
        n_bodies = comp.bRepBodies.count
        ok_solids = (report.get('solid_bodies') == 6 and n_bodies == 6
                     and not report['problems'])
        failures += 0 if ok_solids else 1
        lines.append('  %d bodies, %d bearing joints, problems: %s  %s'
                     % (n_bodies, report.get('solid_joints', 0),
                        report['problems'] or 'none',
                        'ok' if ok_solids else 'FAIL'))

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
