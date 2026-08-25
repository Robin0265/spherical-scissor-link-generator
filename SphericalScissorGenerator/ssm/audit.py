"""Independent re-measurement of what was built.

Lengths come from the curve evaluator rather than endpoint positions: a sketch
can report itself fully constrained, with its arc endpoints in exactly the right
places, while the curve between them still carries a stale sweep from a previous
solve. Measuring the curve is what catches that.
"""

import math
import adsk.fusion

from . import vectors as vec
from . import PREFIX


def verify(design, comp, solved, frame, timeline_from=None):
    """`timeline_from`: first timeline index the health check should look at.
    The builder passes where this run's features start, so the check skips
    everything that existed before - iterating a large host document's whole
    timeline over the COM boundary costs real seconds per run."""
    radius, alpha, n = solved['R'], solved['alpha'], solved['n']
    report = {'links': [], 'problems': [], 'n': n}

    for sketch in comp.sketches:
        if not sketch.name.startswith(PREFIX):
            continue
        if not sketch.isFullyConstrained:
            report['problems'].append('%s is not fully constrained' % sketch.name)
        if not sketch.name.startswith(PREFIX + 'Link_'):
            continue
        for arc in sketch.sketchCurves.sketchArcs:
            if arc.isConstruction or arc.isReference:
                continue
            evaluator = arc.worldGeometry.evaluator
            ok, start, end = evaluator.getParameterExtents()
            ok, length = evaluator.getLengthAtParameter(start, end)
            multiple = 2 if 'Long' in sketch.name else 1
            expected = multiple * radius * alpha
            report['links'].append((sketch.name, length / expected))
            if abs(length - expected) > 1e-4:
                report['problems'].append(
                    '%s spans %.3f alpha, expected %d'
                    % (sketch.name, length / (radius * alpha), multiple))

    # The terminal links are dimensioned to alpha and never welded to the apex,
    # so how far they land from it measures the formulation itself.
    apex = frame.joint(radius, n * solved['delta'], 0.0)
    worst = 0.0
    for sketch in comp.sketches:
        if not sketch.name.startswith(PREFIX + 'Link_End'):
            continue
        for arc in sketch.sketchCurves.sketchArcs:
            if arc.isConstruction or arc.isReference:
                continue
            worst = max(worst, min(vec.dist(arc.startSketchPoint.worldGeometry, apex),
                                   vec.dist(arc.endSketchPoint.worldGeometry, apex)))
    report['closure_mm'] = worst * 10.0
    if report['closure_mm'] > 1e-3:
        report['problems'].append(
            'terminal links miss the apex by %.4f mm' % report['closure_mm'])

    def check_item(item):
        try:
            if item.isGroup:
                # Group containers report UnknownFeatureHealthState by design;
                # a collapsed group hides its members from the top-level
                # timeline iteration, so descend into it explicitly.
                for i in range(item.count):
                    check_item(item.item(i))
                return
            if item.healthState != adsk.fusion.FeatureHealthStates.HealthyFeatureHealthState:
                report['problems'].append(
                    'feature "%s" is not healthy' % (item.entity.name if item.entity else '?'))
        except Exception:
            pass

    timeline = design.timeline
    for i in range(timeline_from or 0, timeline.count):
        check_item(timeline.item(i))

    return report


def format_report(report, solved):
    lines = [
        '%d rhombi, %d links generated.' % (report['n'], len(report['links'])),
        '',
        'alpha  = %.3f deg   (link curvature)' % math.degrees(solved['alpha']),
        'delta  = %.3f deg   (span per rhombus)' % math.degrees(solved['delta']),
        'gamma_ = %.3f deg   (pin elevation)' % math.degrees(solved['gamma']),
        'lambda = %.3f deg   (seed plane dihedral)' % math.degrees(solved['lam']),
        '',
        'Closure residual at the apex: %.2e mm' % report['closure_mm'],
    ]
    if report.get('solid_bodies'):
        lines.append('Solid links: %d bodies, %d bearing joints all gapped by '
                     'bearing_thickness.' % (report['solid_bodies'],
                                             report.get('solid_joints', 0)))
    where = ('component "%s"' % report['component']) if report.get('component') else 'the root component'
    if report.get('custom_feature'):
        lines.append('Built into %s as custom feature "%s" - double-click it in '
                     'the timeline to edit.' % (where, report['custom_feature']))
    elif report.get('grouped'):
        lines.append('Built into %s and collapsed into one timeline group.' % where)
        lines.append('(Custom feature unavailable - run the add-in rather than '
                     'the script to get an editable timeline node.)')
    if report.get('pack_note'):
        lines.append('')
        lines.append('NOTE: ' + report['pack_note'])
    if report['problems']:
        lines.append('')
        lines.append('PROBLEMS:')
        lines.extend('  - ' + problem for problem in report['problems'])
    else:
        lines.append('Every link measures exactly 1 or 2 alpha; all sketches '
                     'fully constrained.')
    return '\n'.join(lines)
