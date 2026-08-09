"""The Fusion command dialog, shared by the Script and the Add-In entry points.

Both entry points do the same thing: register a command definition whose
commandCreated handler builds this dialog. The Script runs it once on demand;
the Add-In also owns a toolbar button and, at startup, the CustomFeature
definition that lets a generated mechanism collapse into a single editable
feature.

Nothing here touches the document until the user presses Generate - validation
and the live preview both run off `parameters.solve_expressions`, which solves
from expressions alone.
"""

import traceback

import adsk.core
import adsk.fusion

from . import PREFIX
from . import audit, builder, parameters

_app = None
_ui = None
_handlers = []


def _bind(app, ui):
    """Give this module the application objects its handlers use."""
    global _app, _ui
    _app = app
    _ui = ui


CMD_ID = 'sphericalScissorGeneratorCmd'
CMD_NAME = 'Spherical Scissor Generator'

OK_HTML = '<span style="color:#1a7f37">%s</span>'
ERROR_HTML = '<span style="color:#b3261e">%s</span>'


def _selected(inputs, input_id):
    """The single selected entity, or None while the dialog is incomplete."""
    selection = inputs.itemById(input_id)
    if selection is None or selection.selectionCount < 1:
        return None
    return selection.selection(0).entity


def _gather(inputs):
    """Read the dialog into build() arguments."""
    overrides = {}
    for name, _default, _units, _comment in parameters.DRIVING_PARAMS:
        field = inputs.itemById('p_' + name)
        if field is None:
            continue
        overrides[name] = str(int(field.value)) if name == 'n' else field.expression

    direction = inputs.itemById('direction')
    clockwise = bool(direction and direction.selectedItem
                     and direction.selectedItem.name.startswith('Clockwise'))

    return {
        'centre_ent': _selected(inputs, 'centrePoint'),
        'spine_plane_ent': _selected(inputs, 'spinePlane'),
        'start_plane_ent': _selected(inputs, 'startPlane'),
        'overrides': overrides,
        'clockwise': clockwise,
        'flip_start': inputs.itemById('flipStart').value,
        'purge': inputs.itemById('purge').value,
        'pack': inputs.itemById('pack').value,
    }


def _problem(design, args):
    """Why the current dialog state cannot build, or None if it can.

    Checks expressions and plane geometry without touching the document, so it
    is safe to call on every keystroke.
    """
    if args['centre_ent'] is None:
        return 'Select the sphere centre.'
    if args['spine_plane_ent'] is None:
        return 'Select the spine plane.'
    if args['start_plane_ent'] is None:
        return 'Select the start plane.'
    try:
        parameters.solve_expressions(design, args['overrides'])
        from . import frame as frame_mod
        from . import vectors as vec
        frame_mod.resolve(vec.point_of(args['centre_ent']),
                          args['spine_plane_ent'], args['start_plane_ent'],
                          args['clockwise'], args['flip_start'])
    except RuntimeError as err:
        return str(err)
    except Exception as err:  # malformed selection, etc.
        return str(err)
    return None


def _set_status(inputs, message, is_error):
    box = inputs.itemById('status')
    if box is None:
        return
    template = ERROR_HTML if is_error else OK_HTML
    box.formattedText = template % message.replace('\n', '<br />')


def _build(design, args):
    return builder.build(
        design, args['centre_ent'], args['spine_plane_ent'], args['start_plane_ent'],
        overrides=args['overrides'], clockwise=args['clockwise'],
        flip_start=args['flip_start'], purge=args['purge'], pack=args['pack'])


class ValidateHandler(adsk.core.ValidateInputsEventHandler):
    def notify(self, args):
        try:
            inputs = args.inputs
            design = adsk.fusion.Design.cast(_app.activeProduct)
            problem = _problem(design, _gather(inputs))
            args.areInputsValid = problem is None
        except Exception:
            args.areInputsValid = False


class InputChangedHandler(adsk.core.InputChangedEventHandler):
    def notify(self, args):
        try:
            inputs = args.inputs
            design = adsk.fusion.Design.cast(_app.activeProduct)
            problem = _problem(design, _gather(inputs))
            if problem:
                _set_status(inputs, problem, True)
            else:
                solved = parameters.solve_expressions(design, _gather(inputs)['overrides'])
                import math
                _set_status(inputs,
                            'Ready: %d rhombi, %d links. alpha %.2f&deg;, delta %.2f&deg;, '
                            'gamma %.2f&deg;, lambda %.2f&deg;'
                            % (solved['n'], 2 * solved['n'] + 2,
                               math.degrees(solved['alpha']), math.degrees(solved['delta']),
                               math.degrees(solved['gamma']), math.degrees(solved['lam'])),
                            False)
        except Exception:
            pass


class PreviewHandler(adsk.core.CommandEventHandler):
    def notify(self, args):
        try:
            inputs = args.command.commandInputs
            if not inputs.itemById('preview').value:
                return
            design = adsk.fusion.Design.cast(_app.activeProduct)
            gathered = _gather(inputs)
            if _problem(design, gathered) is not None:
                return
            _build(design, gathered)
            # Let the real execute run so the audit report is produced from a
            # clean build rather than from rolled-back preview geometry.
            args.isValidResult = False
        except Exception:
            # A preview must never interrupt the dialog with a dialog.
            pass


class ExecuteHandler(adsk.core.CommandEventHandler):
    def notify(self, args):
        try:
            inputs = args.command.commandInputs
            design = adsk.fusion.Design.cast(_app.activeProduct)
            if not design:
                _ui.messageBox('Open a Fusion design first.', CMD_NAME)
                return

            gathered = _gather(inputs)
            report = _build(design, gathered)
            solved = parameters.read_solved(design)
            _ui.messageBox(audit.format_report(report, solved), CMD_NAME)
        except RuntimeError as err:
            _ui.messageBox(str(err), CMD_NAME + ' - cannot build')
        except Exception:
            if _ui:
                _ui.messageBox('Failed:\n{}'.format(traceback.format_exc()))


class DestroyHandler(adsk.core.CommandEventHandler):
    """Ends the script when run as a Script.

    An add-in must stay loaded after its dialog closes, so terminating is opt-in
    rather than automatic.
    """

    def __init__(self, terminate_on_destroy):
        super(DestroyHandler, self).__init__()
        self._terminate = terminate_on_destroy

    def notify(self, args):
        if self._terminate:
            adsk.terminate()


class CreatedHandler(adsk.core.CommandCreatedEventHandler):
    def __init__(self, terminate_on_destroy=False):
        super(CreatedHandler, self).__init__()
        self._terminate = terminate_on_destroy

    def notify(self, args):
        try:
            cmd = args.command
            cmd.okButtonText = 'Generate'
            cmd.setDialogInitialSize(440, 700)
            inputs = cmd.commandInputs

            centre = inputs.addSelectionInput(
                'centrePoint', 'Sphere centre', 'Centre of rotation (RCM)')
            for filt in ('ConstructionPoints', 'SketchPoints', 'Vertices'):
                centre.addSelectionFilter(filt)
            centre.setSelectionLimits(1, 1)

            spine = inputs.addSelectionInput(
                'spinePlane', 'Spine plane',
                'Plane the rhombi chain runs in; must contain the centre')
            for filt in ('ConstructionPlanes', 'PlanarFaces'):
                spine.addSelectionFilter(filt)
            spine.setSelectionLimits(1, 1)

            start = inputs.addSelectionInput(
                'startPlane', 'Start plane',
                'Where the chain begins. The first joint sits on this plane and '
                'the spine leaves it at a right angle, so it must be '
                'perpendicular to the spine plane and contain the centre.')
            for filt in ('ConstructionPlanes', 'PlanarFaces'):
                start.addSelectionFilter(filt)
            start.setSelectionLimits(1, 1)

            direction = inputs.addDropDownCommandInput(
                'direction', 'Direction', adsk.core.DropDownStyles.TextListDropDownStyle)
            direction.listItems.add('Counter-clockwise', True)
            direction.listItems.add('Clockwise', False)
            direction.tooltip = ('Which way the chain fans out from the start '
                                 'plane, seen looking along the spine plane normal.')

            design = adsk.fusion.Design.cast(_app.activeProduct)

            def default_for(name, fallback):
                if design:
                    existing = design.userParameters.itemByName(name)
                    if existing:
                        return existing.expression
                return fallback

            group = inputs.addGroupCommandInput('geom', 'Design parameters')
            for name, default, units, _comment in parameters.DRIVING_PARAMS:
                expression = default_for(name, default)
                if name == 'n':
                    try:
                        current = int(float(expression))
                    except ValueError:
                        current = 3
                    group.children.addIntegerSpinnerCommandInput(
                        'p_n', 'n  (number of rhombi)', 1, 24, 1, current)
                else:
                    group.children.addValueInput(
                        'p_' + name, name, units,
                        adsk.core.ValueInput.createByString(expression))

            options = inputs.addGroupCommandInput('opts', 'Options')
            options.children.addBoolValueInput(
                'flipStart', 'Start from the opposite side', True, '', False)
            options.children.addBoolValueInput(
                'pack', 'Pack into one component + timeline group', True, '', True)
            options.children.addBoolValueInput(
                'purge', 'Delete features from previous runs (%s*)' % PREFIX,
                True, '', True)
            options.children.addBoolValueInput(
                'preview', 'Live preview', True, '', True)
            options.isExpanded = False

            inputs.addTextBoxCommandInput('status', '', '', 3, True)

            for event, handler in ((cmd.execute, ExecuteHandler()),
                                   (cmd.executePreview, PreviewHandler()),
                                   (cmd.destroy, DestroyHandler(self._terminate)),
                                   (cmd.validateInputs, ValidateHandler()),
                                   (cmd.inputChanged, InputChangedHandler())):
                event.add(handler)
                _handlers.append(handler)

            _set_status(inputs, 'Select a centre point, a spine plane, and a '
                                'start plane.', False)
        except Exception:
            if _ui:
                _ui.messageBox('Failed:\n{}'.format(traceback.format_exc()))


def resource_folder():
    """The icon folder (…/SphericalScissorGenerator/resources/generate).

    Icons are generated from the mechanism's own geometry by
    tools/make_icons.py. Returns '' when absent so registration still works.
    """
    import os
    folder = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        'resources', 'generate')
    return folder if os.path.isdir(folder) else ''


def register(app, ui, terminate_on_destroy=False):
    """Create (or replace) the command definition and return it.

    Both entry points call this; only the Script asks for termination when the
    dialog closes.
    """
    _bind(app, ui)
    existing = ui.commandDefinitions.itemById(CMD_ID)
    if existing:
        existing.deleteMe()
    definition = ui.commandDefinitions.addButtonDefinition(
        CMD_ID, CMD_NAME,
        'Generate a parametric spherical scissor linkage sketch skeleton',
        resource_folder())
    created = CreatedHandler(terminate_on_destroy)
    definition.commandCreated.add(created)
    _handlers.append(created)
    return definition


def unregister(ui):
    """Remove the command definition. Safe to call when it is already gone."""
    try:
        existing = ui.commandDefinitions.itemById(CMD_ID)
        if existing:
            existing.deleteMe()
    except Exception:
        pass
    del _handlers[:]


def check_design(app, ui):
    """Return the active parametric Design, or None after telling the user why."""
    design = adsk.fusion.Design.cast(app.activeProduct)
    if not design:
        ui.messageBox('Open a Fusion design first.', CMD_NAME)
        return None
    if design.designType != adsk.fusion.DesignTypes.ParametricDesignType:
        ui.messageBox(
            'This generator needs a parametric design.\n'
            'Switch the document to "Capture Design History" first.', CMD_NAME)
        return None
    return design
