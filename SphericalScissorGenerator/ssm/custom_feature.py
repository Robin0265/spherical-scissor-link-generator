"""CustomFeature support: the definition, and wrapping a build into one.

Autodesk's documentation requires the CustomFeatureDefinition to be created in
the add-in's run() - at startup, not inside a command. A Script cannot do this,
which is why creating a custom feature from the script entry point fails with
"make params invalid". Everything here therefore degrades quietly when the
definition is absent, so the script keeps working exactly as before.

`STORE` holds the definition and its compute handler for the life of the
add-in. Fusion event handlers are garbage-collected if nothing keeps a
reference, so this is load-bearing rather than bookkeeping.
"""

import os

import adsk.core
import adsk.fusion

from . import COMPONENT_NAME

DEFINITION_ID = 'ssm.sphericalScissorLinkage'

STORE = {}


class _ComputeHandler(adsk.fusion.CustomFeatureEventHandler):
    """Recompute hook.

    The grouped features are ordinary parametric sketches and construction
    geometry driven by user parameters, so Fusion already re-solves them; there
    is nothing to rebuild here. The handler still has to exist for the feature
    to participate properly in timeline computation.
    """

    def __init__(self):
        super(_ComputeHandler, self).__init__()

    def notify(self, args):
        pass


def register_definition(app, icon_folder=None):
    """Create the definition. Call once, from the add-in's run().

    Returns the definition, or None if creation failed (in which case callers
    fall back to the timeline group).
    """
    if 'definition' in STORE:
        return STORE['definition']

    # create() rejects a missing folder, so fall back to one that exists.
    folder = icon_folder if icon_folder and os.path.isdir(icon_folder) else None
    if folder is None:
        folder = os.path.dirname(os.path.abspath(__file__))

    try:
        definition = adsk.fusion.CustomFeatureDefinition.create(
            DEFINITION_ID, COMPONENT_NAME, folder)
        definition.defaultName = COMPONENT_NAME
    except Exception:
        return None

    handler = _ComputeHandler()
    try:
        definition.customFeatureCompute.add(handler)
    except Exception:
        pass

    STORE['definition'] = definition
    STORE['handler'] = handler
    return definition


def set_edit_command(command_id):
    """Make double-clicking the feature in the timeline open our dialog."""
    definition = STORE.get('definition')
    if definition is None:
        return False
    try:
        definition.editCommandId = command_id
        return True
    except Exception:
        return False


def forget_definition():
    STORE.clear()


def is_available():
    return STORE.get('definition') is not None


def wrap(design, root, component, start_index, solved, overrides):
    """Group the timeline range into one custom feature.

    Returns the CustomFeature, or None if unavailable or refused - the caller
    then falls back to a timeline group. Never raises: a failure here must not
    lose an otherwise correct build.

    Which component owns the feature is not obvious for a packed build: the
    sketches live in the sub-component while the occurrence that created it
    lives in the root timeline. Both are attempted, widest first.
    """
    definition = STORE.get('definition')
    if definition is None:
        return None

    timeline = design.timeline
    try:
        timeline.moveToEnd()
    except Exception:
        return None
    if timeline.count <= start_index:
        return None

    attempts = []
    if root is not None:
        attempts.append(('root', root, start_index))
    if component is not None and component is not root:
        # Skip the occurrence node: start at the first feature inside it.
        attempts.append(('component', component, min(start_index + 1, timeline.count - 1)))

    for _label, owner, first_index in attempts:
        feature = _try_wrap(timeline, owner, definition, first_index, overrides)
        if feature is not None:
            return feature
    return None


def _try_wrap(timeline, owner, definition, first_index, overrides):
    try:
        first = timeline.item(first_index).entity
        last = timeline.item(timeline.count - 1).entity
        if first is None or last is None:
            return None

        feature_input = owner.features.customFeatures.createInput(definition)
        if feature_input is None:
            return None
        if not feature_input.setStartAndEndFeatures(first, last):
            return None

        # Expose the driving values on the feature itself so they can be edited
        # from the timeline node rather than the global parameter table.
        for name, expression, units in _editable_parameters(overrides):
            feature_input.addCustomParameter(
                name, name, adsk.core.ValueInput.createByString(expression),
                units, True)

        return owner.features.customFeatures.add(feature_input)
    except Exception:
        return None


def _editable_parameters(overrides):
    from . import parameters as params
    out = []
    for name, default, units, _comment in params.DRIVING_PARAMS:
        if name in ('l_offset', 'bar_thickness', 'bar_width', 'bearing_OD'):
            continue  # reserved for the solid stage; not useful on the feature
        out.append((name, (overrides or {}).get(name, default), units))
    return out
