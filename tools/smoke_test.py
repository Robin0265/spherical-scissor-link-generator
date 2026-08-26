"""Offline smoke test for builder.hide_scaffolding / builder.ground_occurrence.

    python3 tools/smoke_test.py

Almost nothing in this repo can be tested outside Fusion, because adsk.core and
friends bind to the live process (see README > Development). These two helpers
are the exception: they only read `.name` and write `.isLightBulbOn` /
`.isGrounded` on whatever they are handed, so ordinary objects stand in for the
API's. adsk is stubbed just far enough for `import ssm.builder` to succeed.

What this covers that the in-Fusion self-test cannot: the refusal paths. An
entity that raises on the light-bulb write, and an occurrence whose isGrounded
silently ignores the write, are both hard to produce in a real document and are
exactly where a "hidden"/"grounded" claim could quietly become a lie.
"""
import sys, types, os

class _Meta(type):
    def __getattr__(cls, name):
        return _stub(name)


def _stub(name, _cache={}):
    """A class permissive enough to be subclassed, instantiated or read."""
    if name not in _cache:
        _cache[name] = _Meta(name, (object,), {
            '__init__': lambda self, *a, **k: None,
            '__getattr__': lambda self, n: _stub(n),
        })
    return _cache[name]


for name in ('adsk', 'adsk.core', 'adsk.fusion'):
    mod = types.ModuleType(name)
    mod.__getattr__ = _stub
    sys.modules[name] = mod
sys.modules['adsk'].core = sys.modules['adsk.core']
sys.modules['adsk'].fusion = sys.modules['adsk.fusion']

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'SphericalScissorGenerator'))
from ssm import builder

class Item(object):
    def __init__(self, name, lit=True):
        self.name, self.isLightBulbOn = name, lit

class Stubborn(Item):            # entity that refuses the write
    def __setattr__(self, k, v):
        if k == 'isLightBulbOn' and 'isLightBulbOn' in self.__dict__:
            raise RuntimeError('read-only')
        object.__setattr__(self, k, v)

class Comp(object):
    def __init__(self):
        self.constructionPlanes = [Item('SSM_Plane_Seed_P'), Item('MyPlane')]
        self.constructionAxes = [Item('SSM_Axis_OA'), Stubborn('SSM_Axis_Locked')]
        self.constructionPoints = [Item('BallCenter')]
        self.sketches = [Item('SSM_Spine'), Item('SSM_Link_Seed_P'),
                         Item('SSM_Axis_Ref'), Item('SSM_Boss_End_P_0'),
                         Item('UserSketch')]

    def by_name(self, name):
        for group in (self.sketches, self.constructionPlanes,
                      self.constructionAxes, self.constructionPoints):
            for item in group:
                if item.name == name:
                    return item
        raise KeyError(name)

fails = []
def check(label, cond):
    print('%-58s %s' % (label, 'ok' if cond else 'FAIL'))
    if not cond:
        fails.append(label)

# default: keep the skeleton, drop everything else
c = Comp()
n = builder.hide_scaffolding(c, hide_sketches=False)
check('hides SSM planes/axes', not c.by_name('SSM_Plane_Seed_P').isLightBulbOn)
check('leaves the user plane alone', c.by_name('MyPlane').isLightBulbOn)
check('leaves non-SSM points alone', c.by_name('BallCenter').isLightBulbOn)
check('keeps the spine visible', c.by_name('SSM_Spine').isLightBulbOn)
check('keeps the link sketches visible', c.by_name('SSM_Link_Seed_P').isLightBulbOn)
check('always hides the axis reference', not c.by_name('SSM_Axis_Ref').isLightBulbOn)
check('always hides solid-stage sketches', not c.by_name('SSM_Boss_End_P_0').isLightBulbOn)
check('leaves the user sketch alone', c.by_name('UserSketch').isLightBulbOn)
# 1 plane + 1 axis + Axis_Ref + Boss; the refusing axis is not counted
check('survives an entity that refuses (count=%d)' % n, n == 4)

# opt in: the skeleton goes too
c2 = Comp()
n2 = builder.hide_scaffolding(c2, hide_sketches=True)
check('hides the spine when asked', not c2.by_name('SSM_Spine').isLightBulbOn)
check('hides the links when asked', not c2.by_name('SSM_Link_Seed_P').isLightBulbOn)
check('user sketch still untouched', c2.by_name('UserSketch').isLightBulbOn)
check('counts the extra two (count=%d)' % n2, n2 == 6)

# re-running with the option off must bring the skeleton back, and only it
c3 = Comp()
builder.hide_scaffolding(c3, hide_sketches=True)
builder.hide_scaffolding(c3, hide_sketches=False)
check('un-hiding restores the spine', c3.by_name('SSM_Spine').isLightBulbOn)
check('un-hiding leaves the axis reference hidden',
      not c3.by_name('SSM_Axis_Ref').isLightBulbOn)

class OccOK(object):
    isGrounded = False
class OccReadOnly(object):
    isGrounded = False
    isGroundToParent = False
    def __setattr__(self, k, v):
        if k == 'isGrounded':
            raise RuntimeError('read-only in this build')
        object.__setattr__(self, k, v)
class OccSilent(object):         # accepts the write, ignores it
    def __setattr__(self, k, v):
        object.__setattr__(self, k, False)
    isGrounded = False
    isGroundToParent = False

o = OccOK()
check('grounds via isGrounded', builder.ground_occurrence(o) and o.isGrounded)
o2 = OccReadOnly()
check('falls back to isGroundToParent',
      builder.ground_occurrence(o2) and o2.isGroundToParent)
check('reports failure when the write is ignored',
      builder.ground_occurrence(OccSilent()) is False)

print()
print('SMOKE: ' + ('all passed' if not fails else 'FAILED %s' % fails))
sys.exit(1 if fails else 0)
