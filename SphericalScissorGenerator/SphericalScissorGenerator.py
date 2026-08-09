# Spherical Scissor Link Generator
#
# Generates the fully-parametric sketch skeleton of an n-rhombi spherical
# scissor (SSM) linkage, after Castro et al., "A compact 3-DOF shoulder
# mechanism constructed with scissors linkages for exoskeleton applications",
# Mechanism and Machine Theory, 2019.
#
# This file is the Fusion command dialog only. The geometry lives in the ssm
# package next to it.

import os
import sys
import traceback

import adsk.core
import adsk.fusion

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

# Fusion keeps modules loaded between runs, so an edited submodule would keep
# executing its old code until Fusion restarts. Drop ours before importing.
for _stale in [name for name in list(sys.modules)
               if name == 'ssm' or name.startswith('ssm.')]:
    del sys.modules[_stale]

from ssm import PREFIX  # noqa: E402
from ssm import audit, builder, parameters  # noqa: E402

_app = None
_ui = None
_handlers = []

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
        from ssm import frame as frame_mod
        from ssm import vectors as vec
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
        flip_start=args['flip_start'], purge=args['purge'])


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
    def notify(self, args):
        adsk.terminate()


class CreatedHandler(adsk.core.CommandCreatedEventHandler):
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
                'purge', 'Delete features from previous runs (%s*)' % PREFIX,
                True, '', True)
            options.children.addBoolValueInput(
                'preview', 'Live preview', True, '', True)
            options.isExpanded = False

            inputs.addTextBoxCommandInput('status', '', '', 3, True)

            for event, handler_cls in ((cmd.execute, ExecuteHandler),
                                       (cmd.executePreview, PreviewHandler),
                                       (cmd.destroy, DestroyHandler)):
                handler = handler_cls()
                event.add(handler)
                _handlers.append(handler)

            validate = ValidateHandler()
            cmd.validateInputs.add(validate)
            _handlers.append(validate)

            changed = InputChangedHandler()
            cmd.inputChanged.add(changed)
            _handlers.append(changed)

            _set_status(inputs, 'Select a centre point, a spine plane, and a '
                                'start plane.', False)
        except Exception:
            if _ui:
                _ui.messageBox('Failed:\n{}'.format(traceback.format_exc()))


def run(context):
    global _app, _ui
    try:
        _app = adsk.core.Application.get()
        _ui = _app.userInterface

        design = adsk.fusion.Design.cast(_app.activeProduct)
        if not design:
            _ui.messageBox('Open a Fusion design first.', CMD_NAME)
            return
        if design.designType != adsk.fusion.DesignTypes.ParametricDesignType:
            _ui.messageBox(
                'This generator needs a parametric design.\n'
                'Switch the document to "Capture Design History" first.', CMD_NAME)
            return

        existing = _ui.commandDefinitions.itemById(CMD_ID)
        if existing:
            existing.deleteMe()
        definition = _ui.commandDefinitions.addButtonDefinition(
            CMD_ID, CMD_NAME,
            'Generate a parametric spherical scissor linkage sketch skeleton')

        created = CreatedHandler()
        definition.commandCreated.add(created)
        _handlers.append(created)

        definition.execute()
        adsk.autoTerminate(False)
    except Exception:
        if _ui:
            _ui.messageBox('Failed:\n{}'.format(traceback.format_exc()))
