"""Generate the toolbar / timeline icons from the mechanism's real geometry.

Computes an n=2 spherical scissor chain with the same closure formulas the
generator uses, projects it orthographically from a pleasant angle, and
rasterizes anti-aliased PNGs (pure stdlib: no PIL needed).

Output: SphericalScissorGenerator/resources/generate/{16x16,32x32,64x64}.png
plus @2x variants. Re-run any time the look should change:

    python tools/make_icons.py
"""

import math
import os
import struct
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(os.path.dirname(HERE), 'SphericalScissorGenerator',
                       'resources', 'generate')

# icon look
STROKE = (58, 74, 94)        # dark slate, reads on light and dark toolbars
JOINT = (32, 42, 56)
SUPER = 4                    # supersampling factor


# ---------------------------------------------------------------- geometry --

def unit(v):
    n = math.sqrt(sum(c * c for c in v))
    return tuple(c / n for c in v)


def slerp(a, b, t):
    dot = max(-1.0, min(1.0, sum(x * y for x, y in zip(a, b))))
    omega = math.acos(dot)
    if omega < 1e-9:
        return a
    sa = math.sin((1 - t) * omega) / math.sin(omega)
    sb = math.sin(t * omega) / math.sin(omega)
    return tuple(sa * x + sb * y for x, y in zip(a, b))


def joint(phi, elevation):
    """Sphere point: phi along the spine from A, elevation toward the pins."""
    ce, se = math.cos(elevation), math.sin(elevation)
    return (ce * math.sin(phi), se, ce * math.cos(phi))


def chain(n=2, span=math.radians(80), beta=math.radians(5),
          span_max=math.radians(135)):
    """All link centrelines of an n-rhombi chain as sampled polylines."""
    alpha = math.acos(math.cos(beta) * math.cos(span_max / (2 * n)))
    delta = span / n
    gamma = math.acos(math.cos(alpha) / math.cos(delta / 2))

    def pin(i, sign):
        return joint((i - 0.5) * delta, sign * gamma)

    def node(i):
        return joint(i * delta, 0.0)

    def arc(a, b, samples=24):
        return [slerp(a, b, t / samples) for t in range(samples + 1)]

    links, joints = [], []
    a0 = node(0)
    joints.append(a0)
    for sign in (1, -1):
        links.append(arc(a0, pin(1, sign)))
        joints.append(pin(1, sign))
    for i in range(1, n):
        joints.append(node(i))
        for sign in (1, -1):
            links.append(arc(pin(i, sign), node(i)) +
                         arc(node(i), pin(i + 1, -sign))[1:])
            joints.append(pin(i + 1, -sign))
    apex = node(n)
    joints.append(apex)
    for sign in (1, -1):
        links.append(arc(pin(n, sign), apex))
    return links, joints


def project(links, joints, tilt=math.radians(8), turn=math.radians(0)):
    """Orthographic projection after tilting the sphere toward the viewer."""
    ct, st = math.cos(tilt), math.sin(tilt)
    cu, su = math.cos(turn), math.sin(turn)

    def to2d(p):
        x, y, z = p
        # turn about the vertical, then tilt toward the camera
        x, z = cu * x + su * z, -su * x + cu * z
        y, z = ct * y - st * z, st * y + ct * z
        return (x, y)

    return ([[to2d(p) for p in poly] for poly in links],
            [to2d(p) for p in joints])


# ------------------------------------------------------------ rasterizing --

def stamp(buf, size, cx, cy, radius, color):
    """Circular stamp with 1px soft edge onto an RGBA float buffer."""
    r_out = radius + 0.8
    x0, x1 = max(0, int(cx - r_out)), min(size - 1, int(cx + r_out) + 1)
    y0, y1 = max(0, int(cy - r_out)), min(size - 1, int(cy + r_out) + 1)
    for y in range(y0, y1 + 1):
        for x in range(x0, x1 + 1):
            d = math.hypot(x + 0.5 - cx, y + 0.5 - cy)
            cov = max(0.0, min(1.0, radius + 0.5 - d))
            if cov <= 0:
                continue
            i = y * size + x
            a = buf[i][3]
            na = a + cov * (1 - a)
            if na <= 0:
                continue
            buf[i] = (
                (buf[i][0] * a + color[0] * cov * (1 - a)) / na,
                (buf[i][1] * a + color[1] * cov * (1 - a)) / na,
                (buf[i][2] * a + color[2] * cov * (1 - a)) / na,
                na)


def draw_polyline(buf, size, pts, width, color):
    radius = width / 2.0
    for (ax, ay), (bx, by) in zip(pts, pts[1:]):
        steps = max(1, int(math.hypot(bx - ax, by - ay) / (radius * 0.55)))
        for s in range(steps + 1):
            t = s / steps
            stamp(buf, size, ax + t * (bx - ax), ay + t * (by - ay),
                  radius, color)


def render(size, links2d, joints2d):
    hi = size * SUPER
    buf = [(0.0, 0.0, 0.0, 0.0)] * (hi * hi)

    xs = [p[0] for poly in links2d for p in poly]
    ys = [p[1] for poly in links2d for p in poly]
    span = max(max(xs) - min(xs), max(ys) - min(ys))
    margin = 0.10 * hi
    scale = (hi - 2 * margin) / span
    ox = (min(xs) + max(xs)) / 2
    oy = (min(ys) + max(ys)) / 2

    def toc(p):
        return ((p[0] - ox) * scale + hi / 2,
                hi / 2 - (p[1] - oy) * scale)

    stroke_w = max(1.15 * SUPER, hi * 0.055)
    for poly in links2d:
        draw_polyline(buf, hi, [toc(p) for p in poly], stroke_w, STROKE)
    joint_r = stroke_w * 0.82
    for p in joints2d:
        cx, cy = toc(p)
        stamp(buf, hi, cx, cy, joint_r, JOINT)

    # box-filter downsample
    out = bytearray()
    n2 = SUPER * SUPER
    for y in range(size):
        for x in range(size):
            r = g = b = a = 0.0
            for sy in range(SUPER):
                for sx in range(SUPER):
                    pr, pg, pb, pa = buf[(y * SUPER + sy) * hi + x * SUPER + sx]
                    r += pr * pa
                    g += pg * pa
                    b += pb * pa
                    a += pa
            if a > 0:
                out += bytes((int(r / a + 0.5), int(g / a + 0.5),
                              int(b / a + 0.5), int(a / n2 * 255 + 0.5)))
            else:
                out += b'\x00\x00\x00\x00'
    return bytes(out)


def write_png(path, size, rgba):
    def chunk(tag, data):
        payload = tag + data
        return (struct.pack('>I', len(data)) + payload +
                struct.pack('>I', zlib.crc32(payload) & 0xffffffff))

    raw = b''.join(b'\x00' + rgba[y * size * 4:(y + 1) * size * 4]
                   for y in range(size))
    png = (b'\x89PNG\r\n\x1a\n' +
           chunk(b'IHDR', struct.pack('>IIBBBBB', size, size, 8, 6, 0, 0, 0)) +
           chunk(b'IDAT', zlib.compress(raw, 9)) +
           chunk(b'IEND', b''))
    with open(path, 'wb') as fh:
        fh.write(png)


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    links, joints = chain()
    links2d, joints2d = project(links, joints)

    images = {}
    for size in (16, 32, 64):
        images[size] = render(size, links2d, joints2d)
        write_png(os.path.join(OUT_DIR, '%dx%d.png' % (size, size)),
                  size, images[size])

    # @2x variants reuse the larger renders
    for base, big in ((16, 32), (32, 64)):
        write_png(os.path.join(OUT_DIR, '%dx%d@2x.png' % (base, base)),
                  big, images[big])

    print('icons written to', OUT_DIR)
    for name in sorted(os.listdir(OUT_DIR)):
        print('  %-14s %6d bytes' %
              (name, os.path.getsize(os.path.join(OUT_DIR, name))))


if __name__ == '__main__':
    main()
