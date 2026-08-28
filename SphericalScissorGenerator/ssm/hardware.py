"""McMaster hardware (thrust bearings, bushings) imported at the joints.

Each part's STEP comes from the mcmaster cache (warmed by the dialog's Fetch),
is imported ONCE, and is then repeated across the joints as occurrences of the
same component - one import feature per part number, so the timeline cost
stays flat in n.

Placement is static by design: the occurrences are positioned from the
closed-form solution at generation time and do not follow later parameter
edits - re-run the generator to reposition them, the same answer the tool
already gives for the constraint-branch hazard. What makes the transforms
robust is self-calibration: the part's own axis and axial midpoint are
measured from its largest cylindrical face and its end faces after import, so
McMaster's modelling conventions (axis direction, origin at an end or the
middle) never matter.

Radial layout, from the solids stage's boss recipe: mating bosses gap by
bearing_thickness centred on the sphere, so the bearing's axial midpoint sits
exactly at radius R; each boss spans from R +/- bearing_thickness/2 outward
by bar_thickness + 2*l_offset, so its press-fit bushing centres at
R +/- (bearing_thickness/2 + (bar_thickness + 2*l_offset)/2).
"""

import adsk.core
import adsk.fusion

from . import PREFIX, progress
from . import mcmaster


def joint_sites(solved):
    """(phi, elevation) of every joint - 3n+1 in total.

    A at the start, the 2n pins at +/-gamma, the n-1 middle-link crossings on
    the spine, and the apex. Every one mates an IN and an OUT boss.
    """
    delta, gamma, n = solved['delta'], solved['gamma'], solved['n']
    sites = [(0.0, 0.0)]
    for i in range(1, n + 1):
        sites.append(((i - 0.5) * delta, +gamma))
        sites.append(((i - 0.5) * delta, -gamma))
    for i in range(1, n):
        sites.append((i * delta, 0.0))
    sites.append((n * delta, 0.0))
    return sites


def _import_step(design, comp, path, name):
    """Import a STEP once into comp; returns the new occurrence."""
    manager = adsk.core.Application.get().importManager
    options = manager.createSTEPImportOptions(path)
    before = comp.occurrences.count
    manager.importToTarget(options, comp)
    if comp.occurrences.count <= before:
        raise RuntimeError('%s imported no component.' % path)
    occurrence = comp.occurrences.item(comp.occurrences.count - 1)
    occurrence.component.name = name
    return occurrence


def _part_frame(component):
    """(axis, axial midpoint) of a revolved part, in its own space.

    The axis is the largest cylindrical face's; the extent along it comes
    from the planar end faces (their offset along the axis is exact) plus any
    vertices, which covers washer stacks as well as plain tubes.
    """
    best = None
    for body in component.bRepBodies:
        for face in body.faces:
            cylinder = adsk.core.Cylinder.cast(face.geometry)
            if cylinder and (best is None or cylinder.radius > best[0]):
                best = (cylinder.radius, cylinder.axis, cylinder.origin)
    if best is None:
        raise RuntimeError(
            '%s has no cylindrical face to orient by.' % component.name)
    axis = best[1].copy()
    axis.normalize()
    origin = best[2]

    def along(point):
        return (axis.x * (point.x - origin.x) + axis.y * (point.y - origin.y)
                + axis.z * (point.z - origin.z))

    stations = []
    for body in component.bRepBodies:
        for face in body.faces:
            plane = adsk.core.Plane.cast(face.geometry)
            if plane is not None:
                normal = plane.normal
                dot = abs(axis.x * normal.x + axis.y * normal.y + axis.z * normal.z)
                if dot > 0.99:
                    stations.append(along(face.pointOnFace))
        for vertex in body.vertices:
            stations.append(along(vertex.geometry))
    if not stations:
        raise RuntimeError('%s has no measurable extent.' % component.name)
    mid = (min(stations) + max(stations)) / 2.0
    centre = adsk.core.Point3D.create(origin.x + axis.x * mid,
                                      origin.y + axis.y * mid,
                                      origin.z + axis.z * mid)
    return axis, centre


def _transform(axis_local, centre_local, axis_world, target):
    """The matrix mapping the part's (axis, midpoint) onto (radial, joint)."""
    matrix = adsk.core.Matrix3D.create()
    matrix.setToRotateTo(axis_local, axis_world)
    rotated = centre_local.copy()
    rotated.transformBy(matrix)
    matrix.translation = adsk.core.Vector3D.create(
        target.x - rotated.x, target.y - rotated.y, target.z - rotated.z)
    return matrix


def plan_ticks(hardware, n):
    """How many progress steps place() will take, for reporter.begin."""
    joints = 3 * n + 1
    ticks = 0
    if hardware and hardware.get('bearing_pn'):
        ticks += joints
    if hardware and hardware.get('bushing_pn'):
        ticks += 2 * joints
    return ticks


def place(design, comp, frame, solved, hardware, reporter=None):
    """Import and place the fetched parts at every joint.

    Returns {'bearings': count, 'bushings': count, 'problems': [...]}. A part
    with no cached STEP is skipped with a problem note rather than failing
    the run: the skeleton and solids must never be lost to a missing file.
    """
    report = {'bearings': 0, 'bushings': 0, 'problems': []}
    radius = solved['R']
    boss_height = solved['bar_thickness'] + 2.0 * solved['l_offset']
    bushing_offset = solved['bearing_thickness'] / 2.0 + boss_height / 2.0
    sites = joint_sites(solved)
    centre = frame.C

    for kind, offsets in (('bearing', (0.0,)),
                          ('bushing', (+bushing_offset, -bushing_offset))):
        part_number = (hardware.get(kind + '_pn') or '').strip().upper()
        if not part_number:
            continue
        step = mcmaster.cached_step(part_number)
        if step is None:
            report['problems'].append(
                'hardware: no cached STEP for %s %s - press Fetch in the '
                'dialog first.' % (kind, part_number))
            continue
        occurrence = _import_step(design, comp, step,
                                  PREFIX + 'Hw_' + part_number)
        part = occurrence.component
        axis_local, centre_local = _part_frame(part)

        placed = 0
        first = occurrence
        for phi, elevation in sites:
            joint = frame.joint(radius, phi, elevation)
            radial = adsk.core.Vector3D.create(
                (joint.x - centre.x) / radius, (joint.y - centre.y) / radius,
                (joint.z - centre.z) / radius)
            for offset in offsets:
                target = adsk.core.Point3D.create(
                    centre.x + radial.x * (radius + offset),
                    centre.y + radial.y * (radius + offset),
                    centre.z + radial.z * (radius + offset))
                matrix = _transform(axis_local, centre_local, radial, target)
                if first is not None:
                    first.transform2 = matrix
                    first = None
                else:
                    comp.occurrences.addExistingComponent(part, matrix)
                placed += 1
                progress.tick(reporter, '%s %s at joint' % (kind, part_number))
        report[kind + 's'] = placed

    # Moving the imported occurrence onto its joint leaves a pending position;
    # snapshot it so the timeline stays healthy.
    try:
        if design.snapshots.hasPendingSnapshot:
            design.snapshots.add()
    except Exception:
        pass
    return report
