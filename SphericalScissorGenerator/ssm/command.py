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

import math
import threading
import time
import traceback
import webbrowser

import adsk.core
import adsk.fusion

from . import PREFIX, VERSION
from . import audit, builder, custom_feature, draft, mcmaster, parameters, progress

_app = None
_ui = None
_handlers = []
# Handlers for the currently-open dialog. Kept separate from _handlers so each
# new dialog can drop its predecessor's five handlers instead of accumulating
# them for the whole life of the add-in.
_dialog_handlers = []

# Dimensions fetched from McMaster for the current dialog, keyed 'bearing' /
# 'bushing', plus the part numbers they came from. l_offset re-derives from
# the bushing whenever bar_thickness changes, so the fetch has to outlive the
# event that applied it.
_fetched = {'bearing': None, 'bushing': None,
            'bearing_pn': '', 'bushing_pn': ''}

# True while _run_fetch is pumping its progress dialog - the pump dispatches
# UI events, so a second Fetch click during a lookup must be ignored.
_fetch_running = False

# Attribute group/names used to remember part numbers in the document.
ATTR_GROUP = 'SSM'


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

    def part_number(input_id):
        field = inputs.itemById(input_id)
        return (field.value or '').strip().upper() if field else ''

    return {
        'centre_ent': _selected(inputs, 'centrePoint'),
        'spine_plane_ent': _selected(inputs, 'spinePlane'),
        'start_plane_ent': _selected(inputs, 'startPlane'),
        'overrides': overrides,
        'clockwise': clockwise,
        'flip_start': inputs.itemById('flipStart').value,
        'purge': inputs.itemById('purge').value,
        'pack': inputs.itemById('pack').value,
        'solids': inputs.itemById('solids').value,
        'bearing_pn': part_number('mcmBearing'),
        'bushing_pn': part_number('mcmBushing'),
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
        solved = parameters.solve_expressions(design, args['overrides'])
        from . import frame as frame_mod
        from . import vectors as vec
        frame_mod.resolve(vec.point_of(args['centre_ent']),
                          args['spine_plane_ent'], args['start_plane_ent'],
                          args['clockwise'], args['flip_start'])
        # Fetched McMaster parts are authoritative: a conflict between them
        # and the geometry fields disables Generate/Update, exactly like any
        # other invalid input. solve_expressions returns cm.
        if _fetched['bearing'] or _fetched['bushing']:
            sizes_mm = {name: solved[name] * 10.0
                        for name in ('bar_width', 'bar_thickness', 'l_offset',
                                     'bearing_OD', 'bearing_thickness')}
            conflict = mcmaster.hardware_problem(
                _fetched['bearing'], _fetched['bushing'], sizes_mm)
            if conflict:
                return conflict
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


def _build(design, args, with_solids=None, reporter=None):
    """Run the real build. Only ExecuteHandler calls this: the live preview
    never builds - it draws draft custom graphics instead (see PreviewHandler),
    so the document is only touched once, on Generate/Update."""
    if with_solids is None:
        with_solids = args['solids']
    hardware = None
    if _fetched['bearing_pn'] or _fetched['bushing_pn']:
        hardware = {'bearing_pn': _fetched['bearing_pn'],
                    'bushing_pn': _fetched['bushing_pn']}
    return builder.build(
        design, args['centre_ent'], args['spine_plane_ent'], args['start_plane_ent'],
        overrides=args['overrides'], clockwise=args['clockwise'],
        flip_start=args['flip_start'], purge=args['purge'], pack=args['pack'],
        with_solids=with_solids, reporter=reporter, hardware=hardware)


def _input_mm(inputs, input_id):
    """A value input's current magnitude in millimetres (Fusion stores cm)."""
    field = inputs.itemById(input_id)
    return field.value * 10.0 if field else None


def _lookup_worker(bearing_pn, bushing_pn, results):
    """Runs on a worker thread: metadata lookups only, never the Fusion API.

    Each part resolves (or fails) on its own so a found bushing still
    autofills while the bearing is still missing. Results land in the shared
    dict; the main thread is pumping a progress dialog meanwhile.
    """
    for kind, part_number in (('bearing', bearing_pn), ('bushing', bushing_pn)):
        if not part_number:
            continue
        try:
            results[kind] = mcmaster.fetch_hardware(part_number, kind)
        except mcmaster.McMasterError as err:
            results[kind + '_error'] = str(err)
        except Exception:
            results[kind + '_error'] = '%s %s failed:\n%s' % (
                kind, part_number, traceback.format_exc(limit=2))


def _apply_fetched(inputs):
    """Write the fetched dimensions into the parameter fields.

    Returns the status message. Raises McMasterError when the combination is
    geometrically impossible (message is dialog-ready).
    """
    expressions, warnings = mcmaster.hardware_autofill(
        _fetched['bearing'], _fetched['bushing'],
        _input_mm(inputs, 'p_bar_thickness'), _input_mm(inputs, 'p_bar_width'))
    for name, expression in expressions.items():
        field = inputs.itemById('p_' + name)
        if field:
            field.expression = expression
    applied = ', '.join('%s = %s' % pair for pair in sorted(expressions.items()))
    lines = ['McMaster autofill: %s.' % applied]
    for kind in ('bearing', 'bushing'):
        if _fetched[kind]:
            description = _fetched[kind].get('description') or 'part'
            lines.append('%s %s: %s.' % (kind.capitalize(),
                                         _fetched[kind + '_pn'], description))
    lines.extend(warnings)
    return '\n'.join(lines)


def _open_part_pages(inputs):
    """Open the typed part numbers on mcmaster.com in the user's browser.

    Deliberately user-initiated (a button), and it opens the real default
    browser where the user is the human McMaster serves freely - the module
    itself never opens a browser. Parts already resolved locally are skipped,
    so this only ever offers up what still needs downloading.
    """
    urls, skipped = [], []
    for kind, input_id in (('bearing', 'mcmBearing'), ('bushing', 'mcmBushing')):
        part_number = (inputs.itemById(input_id).value or '').strip().upper()
        if not part_number:
            continue
        if mcmaster.cached_step(part_number) or mcmaster.cached_dims(part_number) \
                or mcmaster.find_downloaded_step(part_number):
            skipped.append(part_number)
            continue
        urls.append((part_number, mcmaster.part_url(part_number)))
    if not urls:
        if skipped:
            _set_status(inputs, 'Already available locally: %s. Nothing to '
                        'download.' % ', '.join(skipped), False)
        else:
            _set_status(inputs, 'Enter a part number first.', True)
        return
    for _pn, url in urls:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    _set_status(inputs, 'Opened %s in your browser. Download each 3-D STEP '
                'into Downloads, then press Fetch or Generate.'
                % ', '.join(pn for pn, _u in urls), False)


def _run_fetch(inputs):
    """The Fetch button: metadata lookup with a visible progress dialog.

    The lookup itself runs on a worker thread (a slow connection must not
    freeze Fusion); the main thread animates a progress dialog and pumps the
    event loop until the worker finishes, then applies the dimensions - so
    by the time the dialog closes, the parameters are already updated. CAD
    is never touched here; Generate acquires and imports it.
    """
    global _fetch_running
    if _fetch_running:
        return
    bearing_pn = (inputs.itemById('mcmBearing').value or '').strip().upper()
    bushing_pn = (inputs.itemById('mcmBushing').value or '').strip().upper()
    if not bearing_pn and not bushing_pn:
        _set_status(inputs, 'Enter a thrust bearing and/or bushing part '
                            'number first.', True)
        return

    results = {}
    worker = threading.Thread(target=_lookup_worker,
                              args=(bearing_pn, bushing_pn, results),
                              daemon=True)
    dialog = _ui.createProgressDialog()
    dialog.isCancelButtonShown = False
    _fetch_running = True
    try:
        dialog.show(CMD_NAME, 'Looking up %s…' % ' and '.join(
            pn for pn in (bearing_pn, bushing_pn) if pn), 0, 100, 0)
        worker.start()
        value = 0
        while worker.is_alive():
            value = (value + 2) % 100
            dialog.progressValue = value
            adsk.doEvents()
            time.sleep(0.04)
    finally:
        _fetch_running = False
        try:
            dialog.hide()
        except Exception:
            pass

    lines, errors = [], []
    fetched_any = False
    for kind, part_number in (('bearing', bearing_pn), ('bushing', bushing_pn)):
        if kind in results:
            dims, source = results[kind]
            _fetched[kind] = dims
            _fetched[kind + '_pn'] = part_number
            fetched_any = True
            lines.append('%s %s: metadata from %s.'
                         % (kind.capitalize(), part_number, source))
        error = results.get(kind + '_error')
        if error:
            errors.append(error)
    if fetched_any:
        try:
            lines.append(_apply_fetched(inputs))
        except mcmaster.McMasterError as err:
            errors.append(str(err))
    _set_status(inputs, '\n'.join(lines + errors) or 'Nothing to fetch.',
                bool(errors))


def _auto_resolve(inputs):
    """Pick up locally-available parts (cache, finished downloads) with no
    button press. Quiet by design: a part that is not there yet just stays
    pending. Returns True when new dimensions were applied."""
    resolved = False
    for kind, input_id in (('bearing', 'mcmBearing'), ('bushing', 'mcmBushing')):
        field = inputs.itemById(input_id)
        if field is None:
            continue
        part_number = (field.value or '').strip().upper()
        if not part_number or _fetched[kind + '_pn'] == part_number:
            continue
        try:
            dims, _source = mcmaster.fetch_hardware(
                part_number, kind, local_only=True)
        except mcmaster.McMasterError:
            continue
        _fetched[kind] = dims
        _fetched[kind + '_pn'] = part_number
        resolved = True
    if resolved:
        try:
            _set_status(inputs, _apply_fetched(inputs), False)
        except mcmaster.McMasterError:
            pass  # the conflict reaches the user through validation
    return resolved


def _resolve_for_build(design, gathered):
    """Resolve the typed part numbers at Generate time and fold their
    dimensions into the build's overrides.

    Full resolution (cache, Downloads, API when configured), so pressing
    Generate right after downloading the STEPs needs no Fetch in between.
    Returns notes for the report; raises RuntimeError on a hard conflict,
    before anything touches the document."""
    notes = []
    for kind in ('bearing', 'bushing'):
        part_number = gathered.get(kind + '_pn') or ''
        if not part_number:
            _fetched[kind] = None
            _fetched[kind + '_pn'] = ''
            continue
        if _fetched[kind] is not None and _fetched[kind + '_pn'] == part_number:
            continue
        try:
            dims, source = mcmaster.fetch_hardware(part_number, kind)
            _fetched[kind] = dims
            _fetched[kind + '_pn'] = part_number
            notes.append('%s %s: dimensions resolved from %s at Generate.'
                         % (kind.capitalize(), part_number, source))
        except mcmaster.McMasterError as err:
            notes.append(str(err))
    if _fetched['bearing'] is None and _fetched['bushing'] is None:
        return notes
    try:
        solved = parameters.solve_expressions(design, gathered['overrides'])
        expressions, _warnings = mcmaster.hardware_autofill(
            _fetched['bearing'], _fetched['bushing'],
            solved['bar_thickness'] * 10.0, solved['bar_width'] * 10.0)
        gathered['overrides'].update(expressions)
        solved = parameters.solve_expressions(design, gathered['overrides'])
        sizes_mm = {name: solved[name] * 10.0
                    for name in ('bar_width', 'bar_thickness', 'l_offset',
                                 'bearing_OD', 'bearing_thickness')}
        conflict = mcmaster.hardware_problem(
            _fetched['bearing'], _fetched['bushing'], sizes_mm)
    except mcmaster.McMasterError as err:
        raise RuntimeError(str(err))
    if conflict:
        raise RuntimeError(conflict)
    return notes


def _remember_part_numbers(design, gathered):
    """Persist the typed part numbers on the document, fetched or not, so
    the next dialog offers them again."""
    for name, part_number in (('mcm_bearing', gathered['bearing_pn']),
                              ('mcm_bushing', gathered['bushing_pn'])):
        try:
            existing = design.attributes.itemByName(ATTR_GROUP, name)
            if part_number:
                design.attributes.add(ATTR_GROUP, name, part_number)
            elif existing is not None:
                existing.deleteMe()
        except Exception:
            pass


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
            changed = args.input.id if args.input else ''
            if changed == 'mcmFetch':
                # The bool input is a button: the click flips it True, and
                # resetting it fires this event once more with False, so only
                # the True edge fetches - reacting to both would fetch twice.
                button = inputs.itemById('mcmFetch')
                if button.value:
                    button.value = False
                    _run_fetch(inputs)
                return
            if changed == 'mcmOpen':
                button = inputs.itemById('mcmOpen')
                if button.value:
                    button.value = False
                    _open_part_pages(inputs)
                return
            if changed in ('mcmBearing', 'mcmBushing'):
                # Once the text no longer names the fetched part, that part
                # must stop gating validation (clearing the field is how
                # manual dimensions come back); a typed part that is already
                # cached or downloaded autofills with no button press.
                kind = 'bearing' if changed == 'mcmBearing' else 'bushing'
                typed = (inputs.itemById(changed).value or '').strip().upper()
                if _fetched[kind] is not None and typed != _fetched[kind + '_pn']:
                    _fetched[kind] = None
                    _fetched[kind + '_pn'] = ''
            # Any dialog interaction quietly picks up parts whose download
            # has finished since the last event - no Fetch press needed. On
            # a pickup the status already reports what was applied.
            if _auto_resolve(inputs):
                return
            if changed == 'p_bar_thickness' and _fetched['bushing']:
                # The boss must stay flush with the fetched bushing.
                try:
                    _apply_fetched(inputs)
                except mcmaster.McMasterError as err:
                    _set_status(inputs, str(err), True)
                    return
            design = adsk.fusion.Design.cast(_app.activeProduct)
            gathered = _gather(inputs)
            problem = _problem(design, gathered)
            if problem:
                _set_status(inputs, problem, True)
            else:
                solved = parameters.solve_expressions(design, gathered['overrides'])
                note = (' Solid links build on Generate (not shown in preview).'
                        if gathered['solids'] else '')
                _set_status(inputs,
                            'Ready: %d rhombi, %d links. alpha %.2f&deg;, delta %.2f&deg;, '
                            'gamma %.2f&deg;, lambda %.2f&deg;.%s'
                            % (solved['n'], 2 * solved['n'] + 2,
                               math.degrees(solved['alpha']), math.degrees(solved['delta']),
                               math.degrees(solved['gamma']), math.degrees(solved['lam']),
                               note),
                            False)
        except Exception:
            pass


class PreviewHandler(adsk.core.CommandEventHandler):
    """Draws the draft skeleton as custom graphics - nothing is built.

    A real preview build has to write the new parameter values into the
    document, and on a document with an existing mechanism that dirties (and
    recomputes) every existing feature - solids included - on every
    keystroke, which is what froze the dialog. The draft touches neither the
    document nor the timeline; the real geometry is rebuilt once, on
    Generate/Update.
    """

    def notify(self, args):
        try:
            inputs = args.command.commandInputs
            design = adsk.fusion.Design.cast(_app.activeProduct)
            gathered = _gather(inputs)
            if (not inputs.itemById('preview').value
                    or _problem(design, gathered) is not None):
                draft.clear()
                return
            draft.draw(design, gathered)
            # isValidResult stays False: nothing was built, so the real
            # execute must still run on OK.
        except Exception:
            # A preview must never interrupt the dialog with a dialog.
            pass


def _log_timings(reporter):
    """Write the stage-timing summary to the Text Commands palette.

    Returns True when something was logged. Never raises: this runs in a
    finally block and must not mask the build's own outcome.
    """
    try:
        text = reporter.report()
        if text:
            _app.log('ssm %s\n%s' % (VERSION, text))
            return True
    except Exception:
        pass
    return False


def _hardware_report(solved, report, gathered):
    """The BOM for the McMaster parts, typed or fetched.

    Every joint mates one IN and one OUT boss: 2n+2 joints around the link
    cycle plus n-1 middle-link crossings on the spine, 3n+1 in total. Each
    takes one thrust bearing in the boss gap and one press-fit bushing per
    boss bore.
    """
    bearing_pn = gathered.get('bearing_pn') or ''
    bushing_pn = gathered.get('bushing_pn') or ''
    if not bearing_pn and not bushing_pn:
        return ''
    n = solved['n']
    joints = 3 * n + 1
    lines = ['Hardware BOM (%d joints: %d around the cycle, %d crossings):'
             % (joints, 2 * n + 2, n - 1)]
    if bearing_pn:
        lines.append('  %d x thrust bearing %s (%d placed)'
                     % (joints, bearing_pn,
                        report.get('hardware_bearings', 0)))
    if bushing_pn:
        lines.append('  %d x bushing %s (one per boss bore, %d placed)'
                     % (2 * joints, bushing_pn,
                        report.get('hardware_bushings', 0)))
    return '\n'.join(lines)


class ExecuteHandler(adsk.core.CommandEventHandler):
    def notify(self, args):
        try:
            inputs = args.command.commandInputs
            design = adsk.fusion.Design.cast(_app.activeProduct)
            if not design:
                _ui.messageBox('Open a Fusion design first.', CMD_NAME)
                return

            gathered = _gather(inputs)
            draft.clear()
            # Typed part numbers survive on the document whether or not
            # their dimensions resolve - forgetting them was a bug.
            _remember_part_numbers(design, gathered)
            # Resolve any still-pending part now (cache, Downloads, API):
            # Generate is the moment the user expects the parts to be
            # fetched and imported. Conflicts abort before the document is
            # touched.
            hardware_notes = _resolve_for_build(design, gathered)
            # The reporter keeps Fusion painted and cancellable during the
            # build. The API is single-threaded, so this cannot run in the
            # background - yielding between features is the available lever.
            # It also timestamps every stage; the summary lands in the Text
            # Commands palette even when the build fails or is cancelled.
            reporter = progress.Reporter(_ui, CMD_NAME)
            timed = False
            try:
                report = _build(design, gathered, reporter=reporter)
            finally:
                reporter.end()
                timed = _log_timings(reporter)
            solved = parameters.read_solved(design)
            message = audit.format_report(report, solved)
            hardware_note = _hardware_report(solved, report, gathered)
            if hardware_note:
                message += '\n\n' + hardware_note
            if hardware_notes:
                message += '\n\n' + '\n'.join(hardware_notes)
            if timed:
                message += ('\n\nStage timings were written to the Text '
                            'Commands palette (View > Show Text Commands).')
            _ui.messageBox(message, CMD_NAME)
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
        draft.clear()
        if self._terminate:
            adsk.terminate()


class CreatedHandler(adsk.core.CommandCreatedEventHandler):
    def __init__(self, terminate_on_destroy=False):
        super(CreatedHandler, self).__init__()
        self._terminate = terminate_on_destroy

    def notify(self, args):
        try:
            # Capture the edit target BEFORE creating inputs: activating
            # selection inputs clears the active selection Fusion handed us.
            edited = custom_feature.edit_target(_ui)

            cmd = args.command
            # Fetched dimensions belong to one dialog session: a stale bushing
            # must not keep re-deriving l_offset in the next dialog.
            _fetched.update(bearing=None, bushing=None,
                            bearing_pn='', bushing_pn='')
            cmd.okButtonText = 'Update' if edited else 'Generate'
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
                'solids', 'Build solid links (bars, bosses, bores)', True, '', True)
            options.children.addBoolValueInput(
                'pack', 'Pack into one component + timeline group', True, '', True)
            options.children.addBoolValueInput(
                'purge', 'Delete features from previous runs (%s*)' % PREFIX,
                True, '', True)
            options.children.addBoolValueInput(
                'preview', 'Live preview', True, '', True)
            options.isExpanded = False

            def attr_default(name):
                if design:
                    stored = design.attributes.itemByName(ATTR_GROUP, name)
                    if stored:
                        return stored.value
                return ''

            hardware = inputs.addGroupCommandInput(
                'mcm', 'McMaster-Carr hardware')
            hardware.children.addStringValueInput(
                'mcmBearing', 'Thrust bearing part #',
                attr_default('mcm_bearing'))
            hardware.children.addStringValueInput(
                'mcmBushing', 'Bushing part #', attr_default('mcm_bushing'))
            fetch = hardware.children.addBoolValueInput(
                'mcmFetch', 'Fetch specs and autofill', False, '', False)
            fetch.text = 'Fetch'
            fetch.tooltip = (
                'Fill bearing_OD, bearing_thickness and l_offset from the '
                'parts\' dimensions - retrieved over HTTP from the Product '
                'Information API when configured, else from the local cache '
                'or a STEP already in Downloads. No browser is opened. The '
                'fields stay editable, and Generate imports the parts.')
            open_pages = hardware.children.addBoolValueInput(
                'mcmOpen', 'Open part pages to download STEP', False, '', False)
            open_pages.text = 'Open part pages'
            open_pages.tooltip = (
                'Open the typed part numbers on mcmaster.com in your browser '
                'so you can download each 3-D STEP (free, no login). Drop '
                'them in Downloads and the tool picks them up automatically '
                'at Fetch or Generate. Only needed once per new part.')
            hardware.isExpanded = bool(attr_default('mcm_bearing')
                                       or attr_default('mcm_bushing'))

            inputs.addTextBoxCommandInput('status', '', '', 3, True)

            restored = 0
            if edited is not None:
                # Editing an existing mechanism: restore what the user set up
                # last time. Selections stay until cleared and reselected.
                state = custom_feature.read_state(edited, design)
                for input_id in custom_feature.SELECTION_IDS:
                    entity = state['selections'].get(input_id)
                    selection_input = inputs.itemById(input_id)
                    if entity is not None and selection_input is not None:
                        try:
                            if selection_input.addSelection(entity):
                                restored += 1
                        except Exception:
                            pass
                opts = state['options']
                if opts.get('clockwise'):
                    for i in range(direction.listItems.count):
                        item = direction.listItems.item(i)
                        item.isSelected = item.name.startswith('Clockwise')
                if 'flip_start' in opts:
                    inputs.itemById('flipStart').value = opts['flip_start']
                if 'solids' in opts:
                    inputs.itemById('solids').value = opts['solids']

            # Only one command dialog exists at a time, so the previous
            # dialog's handlers (its command is already destroyed) can go now.
            del _dialog_handlers[:]
            for event, handler in ((cmd.execute, ExecuteHandler()),
                                   (cmd.executePreview, PreviewHandler()),
                                   (cmd.destroy, DestroyHandler(self._terminate)),
                                   (cmd.validateInputs, ValidateHandler()),
                                   (cmd.inputChanged, InputChangedHandler())):
                event.add(handler)
                _dialog_handlers.append(handler)

            if edited is not None:
                if restored == len(custom_feature.SELECTION_IDS):
                    _set_status(inputs, 'Editing the existing mechanism - all '
                                        'previous inputs restored. Change '
                                        'anything and press Update.', False)
                else:
                    _set_status(inputs, 'Editing the existing mechanism - '
                                        'restored %d of %d selections; please '
                                        'reselect the missing ones.'
                                        % (restored,
                                           len(custom_feature.SELECTION_IDS)),
                                True)
            else:
                _set_status(inputs, 'Select a centre point, a spine plane, and '
                                    'a start plane.', False)

            # Remembered part numbers whose data is already local (cache or
            # a finished download) autofill as soon as the dialog opens.
            _auto_resolve(inputs)
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
    del _dialog_handlers[:]


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
