"""Solid link bodies on top of the sketch skeleton.
Formulation (decoded from the user's hand-built reference and verified against
it): each link is a curved bar swept along its link arc, radially offset so the
bar's near face sits at R +/- (l_offset + bearing_thickness/2) - inward (IN) or
outward (OUT) of the joint sphere. At every joint the link carries a boss of
diameter bar_width spanning the bar plus l_offset on each radial side, which
lands the bearing-side boss face at exactly R -/+ bearing_thickness/2: mating
bosses across any joint therefore gap by exactly bearing_thickness, centred on
the sphere. A bearing_OD bore is cut through. Long links get the same boss at
their centre node.
Levels: the link adjacency graph (links as nodes, shared joints as edges) is a
single cycle of length 2n+2 - always even - so alternating IN/OUT around the
cycle is always possible and puts opposite levels at every joint. n=1 needs no
special case: the cycle is simply length 4 with no long links.
Boss recipe (the user's method, API-adapted): a midplane at the bar's
mid-thickness on the joint's radial line; a circle anchored to the projected
joint point and TANGENT to the bar's slice edge (radius = bar_width/2 by
reference, no dimension); a symmetric extrude of bar_thickness + 2*l_offset
(the API cannot reproduce the UI's to-sketch-point extents, and the midplane is
the boss's symmetry plane, so a symmetric extent is exact with no direction
ambiguity); a concentric bearing_OD bore cut through-all, scoped to the body.
"""
import math
import adsk.core
import adsk.fusion
from . import PREFIX
IN, OUT = -1, +1
def _unit(x, y, z):
    n = math.sqrt(x * x + y * y + z * z)
    return (x / n, y / n, z / n)
def _p(x, y, z=0.0):
    return adsk.core.Point3D.create(x, y, z)
def _params(design):
    up = design.userParameters
    def val(name):
        p = up.itemByName(name)
        if p is None:
            raise RuntimeError('Missing parameter "%s" (required by the solid '
                               'stage).' % name)
        return p.value
    return {
        'R': val('link_Radius'),
        'alpha': val('alpha'),
        'bw': val('bar_width'),
        'btk': val('bar_thickness'),
        'loff': val('l_offset'),
        'brg': val('bearing_thickness'),
        'bore': val('bearing_OD'),
    }
# ------------------------------------------------------------- discovery --
def discover_links(comp, prm):
    """All link sketches with their arcs, joint directions, and kind."""
    links = {}
    for sk in comp.sketches:
        if not sk.name.startswith(PREFIX + 'Link_'):
            continue
        arc = None
        for a in sk.sketchCurves.sketchArcs:
            if not a.isConstruction and not a.isReference:
                arc = a
        if arc is None:
            continue
        sw = arc.startSketchPoint.worldGeometry
        ew = arc.endSketchPoint.worldGeometry
        u_s = _unit(sw.x, sw.y, sw.z)
        u_e = _unit(ew.x, ew.y, ew.z)
        span = math.acos(max(-1.0, min(1.0, u_s[0]*u_e[0] + u_s[1]*u_e[1] + u_s[2]*u_e[2])))
        is_long = span > 1.5 * prm['alpha']
        joints = [u_s, u_e]
        if is_long:
            joints.append(_unit(u_s[0]+u_e[0], u_s[1]+u_e[1], u_s[2]+u_e[2]))
        links[sk.name] = {'sketch': sk, 'arc': arc, 'joints': joints,
                         'is_long': is_long}
    if not links:
        raise RuntimeError('No %sLink_* sketches found - generate the skeleton '
                           'first.' % PREFIX)
    return links
def assign_levels(links):
    """Two-colour the link cycle. Anchor: Seed_P is IN (matches the reference
    model). Adjacency = links sharing an end-joint direction."""
    def key(u):
        return (round(u[0], 4), round(u[1], 4), round(u[2], 4))
    by_joint = {}
    for name, info in links.items():
        for u in info['joints'][:2]:  # centre joints join the same two longs
            by_joint.setdefault(key(u), []).append(name)
    adjacency = {name: set() for name in links}
    for members in by_joint.values():
        for a in members:
            for b in members:
                if a != b:
                    adjacency[a].add(b)
    seed = None
    for name in links:
        if name.endswith('Seed_P'):
            seed = name
    if seed is None:
        seed = sorted(links)[0]
    levels = {seed: IN}
    frontier = [seed]
    while frontier:
        current = frontier.pop()
        for neighbour in adjacency[current]:
            if neighbour in levels:
                if levels[neighbour] == levels[current]:
                    raise RuntimeError(
                        'Link graph is not two-colourable (%s vs %s) - '
                        'unexpected topology.' % (current, neighbour))
            else:
                levels[neighbour] = -levels[current]
                frontier.append(neighbour)
    if len(levels) != len(links):
        raise RuntimeError('Link graph is disconnected - unexpected topology.')
    return levels
def _radial_line(link_sketch, u, R):
    """The radial line along direction u in the link's sketch, and its
    on-sphere endpoint."""
    for ln in link_sketch.sketchCurves.sketchLines:
        s1 = ln.startSketchPoint.worldGeometry
        e1 = ln.endSketchPoint.worldGeometry
        v = _unit(e1.x - s1.x, e1.y - s1.y, e1.z - s1.z)
        if abs(abs(v[0]*u[0] + v[1]*u[1] + v[2]*u[2]) - 1.0) < 1e-4:
            anchor = None
            for ep in (ln.startSketchPoint, ln.endSketchPoint):
                w = ep.worldGeometry
                if abs(math.sqrt(w.x**2 + w.y**2 + w.z**2) - R) < 1e-3:
                    anchor = ep
            if anchor is not None:
                return ln, anchor
    raise RuntimeError('%s: no radial line along (%.2f, %.2f, %.2f).'
                       % (link_sketch.name, u[0], u[1], u[2]))
# ------------------------------------------------------------------ bar --
def build_bar(comp, design, info, level, prm, name):
    """Swept bar with the fully-constrained centre-rectangle profile."""
    arc, link_sketch = info['arc'], info['sketch']
    R, bw, btk, loff, brg = (prm['R'], prm['bw'], prm['btk'], prm['loff'],
                             prm['brg'])
    off = loff + brg / 2
    if level == OUT:
        r_near, r_far = R + off, R + off + btk
    else:
        r_near, r_far = R - off, R - off - btk
    u0 = info['joints'][0]
    radial, _anchor = _radial_line(link_sketch, u0, R)
    nrm = arc.worldGeometry.normal
    w = _unit(nrm.x, nrm.y, nrm.z)
    plane_input = comp.constructionPlanes.createInput()
    plane_input.setByDistanceOnPath(arc, adsk.core.ValueInput.createByReal(0.0))
    plane = comp.constructionPlanes.add(plane_input)
    plane.name = PREFIX + 'ProfPlane_' + name
    sk = comp.sketches.add(plane)
    sk.name = PREFIX + 'Prof_' + name
    cons = sk.geometricConstraints
    dims = sk.sketchDimensions
    # corners in sketch space, drawn slightly nudged then constrained
    def corner(r, side):
        wp = _p(r*u0[0] + side*w[0], r*u0[1] + side*w[1], r*u0[2] + side*w[2])
        return sk.modelToSketchSpace(wp)
    c = [corner(r_near, bw/2), corner(r_near, -bw/2),
         corner(r_far, -bw/2), corner(r_far, bw/2)]
    nudges = [(0.02, 0.015), (-0.018, 0.012), (0.016, -0.02), (-0.014, -0.017)]
    lines = []
    for i in range(4):
        a = c[i]
        b = c[(i + 1) % 4]
        na, nb = nudges[i], nudges[(i + 1) % 4]
        lines.append(sk.sketchCurves.sketchLines.addByTwoPoints(
            _p(a.x + na[0], a.y + na[1]), _p(b.x + nb[0], b.y + nb[1])))
    near_side, far_side = lines[0], lines[2]
    # weld the loop
    for i in range(4):
        cons.addCoincident(lines[i].endSketchPoint,
                           lines[(i + 1) % 4].startSketchPoint)
    # shape
    cons.addPerpendicular(lines[1], near_side)
    cons.addParallel(lines[3], lines[1])
    cons.addParallel(far_side, near_side)
    # centre-rectangle bookkeeping: diagonals + centre point
    d1 = sk.sketchCurves.sketchLines.addByTwoPoints(
        _p(c[0].x + 0.03, c[0].y + 0.02), _p(c[2].x - 0.03, c[2].y - 0.02))
    d2 = sk.sketchCurves.sketchLines.addByTwoPoints(
        _p(c[1].x + 0.03, c[1].y - 0.02), _p(c[3].x - 0.03, c[3].y + 0.02))
    for dline in (d1, d2):
        dline.isConstruction = True
    cons.addCoincident(d1.startSketchPoint, lines[0].startSketchPoint)
    cons.addCoincident(d1.endSketchPoint, lines[2].startSketchPoint)
    cons.addCoincident(d2.startSketchPoint, lines[1].startSketchPoint)
    cons.addCoincident(d2.endSketchPoint, lines[3].startSketchPoint)
    centre = sk.sketchPoints.add(_p((c[0].x + c[2].x)/2 + 0.02,
                                    (c[0].y + c[2].y)/2 + 0.01))
    cons.addCoincident(centre, d1)
    cons.addCoincident(centre, d2)
    # radial reference + offset line (origin = the on-sphere joint)
    proj = sk.project(radial).item(0)
    proj.isConstruction = True
    mx, my = (c[0].x + c[1].x)/2, (c[0].y + c[1].y)/2
    l6 = sk.sketchCurves.sketchLines.addByTwoPoints(
        _p(0.03, 0.02), _p(mx*0.9, my*0.9))
    l6.isConstruction = True
    cons.addCoincident(l6.startSketchPoint, sk.originPoint)
    cons.addCollinear(l6, proj)
    cons.addMidPoint(l6.endSketchPoint, near_side)
    cons.addPerpendicular(l6, near_side)
    # dims
    def dim(line, expr, dx, dy):
        s1 = line.startSketchPoint.geometry
        e1 = line.endSketchPoint.geometry
        dm = dims.addDistanceDimension(
            line.startSketchPoint, line.endSketchPoint,
            adsk.fusion.DimensionOrientations.AlignedDimensionOrientation,
            _p((s1.x + e1.x)/2 + dx, (s1.y + e1.y)/2 + dy))
        dm.parameter.expression = expr
    dim(near_side, 'bar_width', 0.3, 0.3)
    dim(lines[1], 'bar_thickness', -0.3, 0.3)
    dim(l6, 'l_offset + bearing_thickness / 2', 0.1, -0.4)
    design.computeAll()
    if not sk.isFullyConstrained:
        raise RuntimeError(sk.name + ' is not fully constrained.')
    path = comp.features.createPath(arc, False)
    sweep_input = comp.features.sweepFeatures.createInput(
        sk.profiles.item(0), path,
        adsk.fusion.FeatureOperations.NewBodyFeatureOperation)
    sweep = comp.features.sweepFeatures.add(sweep_input)
    body = sweep.bodies.item(0)
    body.name = PREFIX + 'Link_' + name
    return body
# ----------------------------------------------------------- boss & bore --
OUT_MID = ('( link_Radius + l_offset + bearing_thickness / 2 + '
           'bar_thickness / 2 ) / link_Radius')
IN_MID = ('( link_Radius - l_offset - bearing_thickness / 2 - '
          'bar_thickness / 2 ) / link_Radius')
def add_boss(comp, design, body, link_sketch, u, level, prm, name, index):
    R, bw, btk, loff, brg = (prm['R'], prm['bw'], prm['btk'], prm['loff'],
                             prm['brg'])
    radial, anchor = _radial_line(link_sketch, u, R)
    s1 = radial.startSketchPoint.worldGeometry
    starts_centre = math.sqrt(s1.x**2 + s1.y**2 + s1.z**2) < R / 2
    base = OUT_MID if level == OUT else IN_MID
    core = base.split(' / link_Radius')[0]
    expr = base if starts_centre else ('( link_Radius - ' + core +
                                       ' ) / link_Radius')
    plane_input = comp.constructionPlanes.createInput()
    plane_input.setByDistanceOnPath(radial,
                                    adsk.core.ValueInput.createByString(expr))
    plane = comp.constructionPlanes.add(plane_input)
    plane.name = '%sMid_%s_%d' % (PREFIX, name, index)
    g = plane.geometry
    r_now = math.sqrt(g.origin.x**2 + g.origin.y**2 + g.origin.z**2)
    want = R + level * (loff + brg/2 + btk/2)
    if abs(r_now - want) > 1e-3:
        raise RuntimeError('%s: midplane at %.3f, expected %.3f'
                           % (plane.name, r_now, want))
    boss_sk = comp.sketches.add(plane)
    boss_sk.name = '%sBoss_%s_%d' % (PREFIX, name, index)
    cut_edges = boss_sk.projectCutEdges(body)
    for i in range(cut_edges.count):
        try:
            cut_edges.item(i).isConstruction = True
        except Exception:
            pass
    proj_anchor = boss_sk.project(anchor).item(0)
    ga = proj_anchor.geometry
    side_line = None
    for i in range(cut_edges.count):
        edge = cut_edges.item(i)
        if edge.objectType.split('::')[-1] != 'SketchLine':
            continue
        s2 = edge.startSketchPoint.geometry
        e2 = edge.endSketchPoint.geometry
        dx, dy = e2.x - s2.x, e2.y - s2.y
        length = math.hypot(dx, dy)
        if length < 1e-6:
            continue
        dist = abs((ga.x - s2.x)*dy - (ga.y - s2.y)*dx) / length
        if abs(dist - bw/2) < 1e-3:
            side_line = edge
            break
    if side_line is None:
        raise RuntimeError(boss_sk.name + ': no slice edge to be tangent to.')
    circle = boss_sk.sketchCurves.sketchCircles.addByCenterRadius(
        _p(ga.x + 0.06, ga.y + 0.04), bw/2 * 0.9)
    boss_sk.geometricConstraints.addCoincident(circle.centerSketchPoint,
                                               proj_anchor)
    boss_sk.geometricConstraints.addTangent(circle, side_line)
    design.computeAll()
    if abs(circle.radius - bw/2) > 1e-4:
        raise RuntimeError(boss_sk.name + ': tangent circle radius wrong.')
    if not boss_sk.isFullyConstrained:
        raise RuntimeError(boss_sk.name + ' is not fully constrained.')
    boss_input = comp.features.extrudeFeatures.createInput(
        boss_sk.profiles.item(0),
        adsk.fusion.FeatureOperations.JoinFeatureOperation)
    boss_input.setSymmetricExtent(
        adsk.core.ValueInput.createByString('bar_thickness + 2 * l_offset'),
        True)
    boss_input.participantBodies = [body]
    comp.features.extrudeFeatures.add(boss_input)
    bore_sk = comp.sketches.add(plane)
    bore_sk.name = '%sBore_%s_%d' % (PREFIX, name, index)
    proj2 = bore_sk.project(anchor).item(0)
    g2 = proj2.geometry
    bore_circle = bore_sk.sketchCurves.sketchCircles.addByCenterRadius(
        _p(g2.x + 0.05, g2.y + 0.03), prm['bore']/2 * 0.9)
    bore_sk.geometricConstraints.addCoincident(bore_circle.centerSketchPoint,
                                               proj2)
    dm = bore_sk.sketchDimensions.addDiameterDimension(
        bore_circle, _p(g2.x + 0.7, g2.y + 0.7))
    dm.parameter.expression = 'bearing_OD'
    if not bore_sk.isFullyConstrained:
        raise RuntimeError(bore_sk.name + ' is not fully constrained.')
    bore_input = comp.features.extrudeFeatures.createInput(
        bore_sk.profiles.item(0),
        adsk.fusion.FeatureOperations.CutFeatureOperation)
    bore_input.setTwoSidesExtent(
        adsk.fusion.ThroughAllExtentDefinition.create(),
        adsk.fusion.ThroughAllExtentDefinition.create())
    bore_input.participantBodies = [body]
    comp.features.extrudeFeatures.add(bore_input)
# ------------------------------------------------------------- assembly --
def build_all(design, comp):
    """Build every link body. Returns an audit report dict."""
    prm = _params(design)
    links = discover_links(comp, prm)
    levels = assign_levels(links)
    for name in sorted(links):
        info = links[name]
        short = name.replace(PREFIX + 'Link_', '')
        body = build_bar(comp, design, info, levels[name], prm, short)
        for idx, u in enumerate(info['joints']):
            add_boss(comp, design, body, info['sketch'], u, levels[name],
                     prm, short, idx)
        design.computeAll()
    return audit_joints(design, comp, prm)
def audit_joints(design, comp, prm):
    """Every joint must mate one IN and one OUT body with a bearing_thickness
    gap centred on the sphere."""
    R, brg, bore_r = prm['R'], prm['brg'], prm['bore']/2
    joints = []
    for body in comp.bRepBodies:
        for face in body.faces:
            g = face.geometry
            if (g.objectType.split('::')[-1] != 'Cylinder'
                    or abs(g.radius - bore_r) > 1e-4):
                continue
            au = _unit(g.axis.x, g.axis.y, g.axis.z)
            if au[2] < 0 or (abs(au[2]) < 1e-6 and
                             (au[0] < 0 or (abs(au[0]) < 1e-6 and au[1] < 0))):
                au = (-au[0], -au[1], -au[2])
            lo, hi = 1e9, -1e9
            for edge in face.edges:
                for vertex in (edge.startVertex, edge.endVertex):
                    p = vertex.geometry
                    t = abs(p.x*au[0] + p.y*au[1] + p.z*au[2])
                    lo, hi = min(lo, t), max(hi, t)
            for j in joints:
                if (abs(au[0]-j[0][0]) < 1e-3 and abs(au[1]-j[0][1]) < 1e-3
                        and abs(au[2]-j[0][2]) < 1e-3):
                    j[1].setdefault(body.name, [1e9, -1e9])
                    j[1][body.name][0] = min(j[1][body.name][0], lo)
                    j[1][body.name][1] = max(j[1][body.name][1], hi)
                    break
            else:
                joints.append((au, {body.name: [lo, hi]}))
    problems = []
    for au, members in joints:
        if len(members) != 2:
            problems.append('joint (%.2f,%.2f,%.2f): %d bodies'
                            % (au[0], au[1], au[2], len(members)))
            continue
        (n1, s1), (n2, s2) = sorted(members.items(), key=lambda kv: kv[1][0])
        gap = s2[0] - s1[1]
        if abs(gap - brg) > 1e-3 or abs((s1[1] + s2[0])/2 - R) > 1e-3:
            problems.append('joint %s/%s: gap %.3f (want %.3f)'
                            % (n1, n2, gap, brg))
    return {'joints': len(joints), 'problems': problems,
            'bodies': comp.bRepBodies.count}