"""Movable links: one component per link, revolute joints at every pin.

The solid stage builds every link as a body of the one mechanism component,
which Fusion treats as a single rigid part. This stage turns it into a
mechanism that can be dragged:

1. Each link body becomes its own component (SSM_Link_<name>) with
   BRepBody.createComponent, which keeps the parametric history. That call
   marks the FIRST new component "Ground to Parent" - silently welding one
   link in place and freezing every loop it closes - so the flag is cleared
   on all of them.
2. Pins are found from the bodies themselves: every boss bore is a cylinder of
   radius bearing_OD/2 whose axis is a radial line through the sphere centre,
   and the two links sharing a pin have coaxial bores. Each pin gets an
   as-built revolute joint (SSM_Pin_<a>_<b>), placed on the bore so its axis is
   exactly the pin axis.
3. Joint A carries the mechanism's two degrees of freedom (from the
   iteration-5 analysis: span + swivel):
     SSM_Swivel  grounded SSM_Frame <-> Seed_P about the input axis - free;
                 turns the whole chain (the spine's direction) about OA.
     SSM_Span    Seed_P <-> Seed_N about the same axis - limited to
                 [span_min, span_max]. The two seed planes sit at
                 +/- lambda(span) about OA (lambda = the `lambda` parameter),
                 so the joint angle is 2*(lambda(span) - lambda(span_target)).
                 The limits keep the scissor out of the flat fold at span 0,
                 a true singular pose the joint solver cannot come back from.
4. A contact set (SSM_Links) over the links, so a drag stops when links touch.

Every axis passes through the sphere centre, so any motion Fusion allows keeps
every link on the sphere - nothing extra is needed to enforce that.

Everything here is after the custom feature in the timeline: a custom feature
cannot span several components (the same limit the McMaster hardware hit), so
the builder wraps the geometry first and groups this stage with it.
"""

import math

import adsk.core
import adsk.fusion

from . import PREFIX

LINK_PREFIX = PREFIX + 'Link_'
FRAME_NAME = PREFIX + 'Frame'
SWIVEL_NAME = PREFIX + 'Swivel'
SPAN_NAME = PREFIX + 'Span'
CONTACT_SET_NAME = PREFIX + 'Links'

# cos tolerance for "same pin axis" (~0.08 deg): mating bores agree to far
# better than this after the sketch solve, while distinct pins are degrees apart.
_PARALLEL = 1.0 - 1e-6
_PROBE = math.radians(0.5)  # span-joint calibration step


def seed_dihedral(span, solved):
    """lambda(span): angle of each seed plane from the spine plane about OA
    (radians). Same formula as the `lambda` parameter, at another span."""
    delta = span / solved['n']
    alpha = solved['alpha']
    c = math.cos(alpha) * (1.0 - math.cos(delta)) / (math.sin(delta) * math.sin(alpha))
    return math.acos(max(-1.0, min(1.0, c)))


def span_joint_value(span, solved, sign):
    """SSM_Span joint angle (radians) that puts the mechanism at `span`."""
    return sign * 2.0 * (seed_dihedral(span, solved)
                         - seed_dihedral(solved['span_target'], solved))


def remove_contact_set(design):
    for contact_set in list(design.contactSets):
        if contact_set.name == CONTACT_SET_NAME:
            try:
                contact_set.deleteMe()
            except Exception:
                pass


def _split_links(comp):
    """createComponent per link body -> {short name: native occurrence}."""
    names = [body.name for body in comp.bRepBodies
             if body.name.startswith(LINK_PREFIX)]
    for name in names:
        body = comp.bRepBodies.itemByName(name)
        moved = body.createComponent()
        if moved is None:
            raise RuntimeError('Could not move %s into a component of its own.' % name)
        moved.parentComponent.name = name
    # Snapshot first: clearing isGroundToParent REORDERS comp.occurrences (the
    # occurrence moves to the end), so changing it while iterating the live
    # collection skipped the next link (End_P, live).
    occurrences = [comp.occurrences.item(i) for i in range(comp.occurrences.count)]
    links = {}
    for occ in occurrences:
        name = occ.component.name
        if name.startswith(LINK_PREFIX):
            links[name[len(LINK_PREFIX):]] = occ
    for occ in links.values():
        if occ.isGroundToParent:
            occ.isGroundToParent = False
    if len(links) != len(names):
        raise RuntimeError('Split %d link bodies into %d components.'
                           % (len(names), len(links)))
    return links


def _pin_sites(proxies, bore_radius, centre):
    """[{'axis': unit Vector3D pointing away from the centre,
         'members': {link: bore face proxy}}], one per pin."""
    sites = []
    for name, proxy in proxies.items():
        for body in proxy.bRepBodies:
            for face in body.faces:
                cylinder = adsk.core.Cylinder.cast(face.geometry)
                if cylinder is None or abs(cylinder.radius - bore_radius) > 1e-5:
                    continue
                axis = cylinder.axis.copy()
                axis.normalize()
                if axis.dotProduct(centre.vectorTo(cylinder.origin)) < 0:
                    axis.scaleBy(-1)
                for site in sites:
                    if site['axis'].dotProduct(axis) > _PARALLEL:
                        site['members'].setdefault(name, face)
                        break
                else:
                    sites.append({'axis': axis, 'members': {name: face}})
    return sites


def _revolute(root, occ_one, occ_two, face, name):
    geometry = adsk.fusion.JointGeometry.createByNonPlanarFace(
        face, adsk.fusion.JointKeyPointTypes.MiddleKeyPoint)
    joint_input = root.asBuiltJoints.createInput(occ_one, occ_two, geometry)
    joint_input.setAsRevoluteJointMotion(adsk.fusion.JointDirections.ZAxisJointDirection)
    joint = root.asBuiltJoints.add(joint_input)
    joint.name = name
    return joint


def _motion(joint, occurrence):
    """The joint's RevoluteJointMotion in the root context (joints live in the
    mechanism component, the lowest common parent of their two links)."""
    for candidate in (lambda: joint.createForAssemblyContext(occurrence),
                      lambda: joint):
        try:
            motion = adsk.fusion.RevoluteJointMotion.cast(candidate().jointMotion)
            if motion is not None:
                return motion
        except Exception:
            continue
    raise RuntimeError('Joint %s has no revolute motion.' % joint.name)


def _measured_span(proxies, bore_radius, centre):
    """The current span: angle between the A and G pin axes."""
    sites = _pin_sites({k: proxies[k] for k in ('Seed_P', 'Seed_N', 'End_P', 'End_N')},
                       bore_radius, centre)
    a = [s['axis'] for s in sites if set(s['members']) == {'Seed_P', 'Seed_N'}]
    g = [s['axis'] for s in sites if set(s['members']) == {'End_P', 'End_N'}]
    if not a or not g:
        raise RuntimeError('Joint A or the apex could not be found.')
    return math.acos(max(-1.0, min(1.0, a[0].dotProduct(g[0]))))


def make_movable(design, occurrence, solved, centre, contact=True):
    """Run the stage on the packed mechanism `occurrence`. Returns a report:
    {'links', 'joints', 'span_limits_deg', 'contact_set', 'problems'}."""
    root = design.rootComponent
    comp = occurrence.component
    report = {'problems': []}
    bore_radius = design.userParameters.itemByName('bearing_OD').value / 2.0

    native = _split_links(comp)
    proxies = {name: occ.createForAssemblyContext(occurrence)
               for name, occ in native.items()}
    report['links'] = len(proxies)

    expected = 3 * solved['n'] + 1
    sites = _pin_sites(proxies, bore_radius, centre)
    bad = [sorted(s['members']) for s in sites if len(s['members']) != 2]
    if len(sites) != expected or bad:
        # Right after createComponent some moved bodies still report their
        # pre-split geometry (seen live: two links' bores missing or out of
        # place, so their pins came out unpaired). One recompute settles it;
        # it is only paid when the first pass is inconsistent, because
        # computeAll is O(whole document).
        design.computeAll()
        sites = _pin_sites(proxies, bore_radius, centre)
        bad = [sorted(s['members']) for s in sites if len(s['members']) != 2]
    if len(sites) != expected or bad:
        raise RuntimeError('Found %d pins (expected %d)%s.' % (
            len(sites), expected, '; unpaired: %s' % bad if bad else ''))

    frame_native = comp.occurrences.addNewComponent(adsk.core.Matrix3D.create())
    frame_native.component.name = FRAME_NAME
    frame = frame_native.createForAssemblyContext(occurrence)
    frame.isGrounded = True

    joints = []
    span_joint = None
    for site in sites:
        names = sorted(site['members'])
        if names == ['Seed_N', 'Seed_P']:
            face = site['members']['Seed_P']
            joints.append(_revolute(root, frame, proxies['Seed_P'], face, SWIVEL_NAME))
            span_joint = _revolute(root, proxies['Seed_P'], proxies['Seed_N'],
                                   face, SPAN_NAME)
            joints.append(span_joint)
        else:
            joints.append(_revolute(root, proxies[names[0]], proxies[names[1]],
                                    site['members'][names[0]],
                                    '%sPin_%s_%s' % (PREFIX, names[0], names[1])))
    report['joints'] = len(joints)

    unhealthy = [j.name for j in joints if j.timelineObject is not None
                 and j.timelineObject.healthState
                 != adsk.fusion.FeatureHealthStates.HealthyFeatureHealthState]
    if unhealthy:
        report['problems'].append('unhealthy joints: %s' % ', '.join(unhealthy))

    # Calibrate the span joint's sign with a small probe, then limit it. The
    # probe also proves the loops actually move: a frozen mechanism (e.g. a
    # link left grounded) reads back 0 and is reported, not silently accepted.
    motion = _motion(span_joint, occurrence)
    built_span = _measured_span(proxies, bore_radius, centre)
    motion.rotationValue = _PROBE
    moved = motion.rotationValue
    probed_span = _measured_span(proxies, bore_radius, centre)
    motion.rotationValue = 0.0
    if abs(moved - _PROBE) > 1e-6:
        report['problems'].append('the mechanism did not move when the span '
                                  'joint was driven - check for grounded links.')
        return report
    change = 2.0 * (seed_dihedral(probed_span, solved)
                    - seed_dihedral(built_span, solved)) / _PROBE
    if abs(abs(change) - 1.0) > 0.05:
        report['problems'].append('span joint calibration is off (%.3f, expected '
                                  '+/-1).' % change)
    sign = 1.0 if change > 0 else -1.0
    ends = sorted((span_joint_value(solved['span_min'], solved, sign),
                   span_joint_value(solved['span_max'], solved, sign)))
    limits = motion.rotationLimits
    limits.isMinimumValueEnabled = True
    limits.minimumValue = ends[0]
    limits.isMaximumValueEnabled = True
    limits.maximumValue = ends[1]
    report['span_limits_deg'] = (math.degrees(solved['span_min']),
                                 math.degrees(solved['span_max']))
    report['span_joint_range_deg'] = (math.degrees(ends[0]), math.degrees(ends[1]))

    if contact:
        remove_contact_set(design)
        contact_set = design.contactSets.add(list(proxies.values()))
        contact_set.name = CONTACT_SET_NAME
        if not design.isContactAnalysisEnabled:
            # Only when it was off: an "all bodies" setting the user chose
            # already covers the links and is left alone.
            design.isContactAnalysisEnabled = True
            design.isContactSetAnalysis = True
        report['contact_set'] = CONTACT_SET_NAME
    return report
