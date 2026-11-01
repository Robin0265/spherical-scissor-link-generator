"""McMaster parts through Fusion's own "Insert McMaster-Carr Component".

Fusion's built-in command is a browser showing mcmaster.com that inserts the
part the user saves into the active design. It has no API: a script can open
it, nothing more. So this module drives the human around it:

1. ``begin`` (from the dialog's Insert button) queues the part numbers that
   are not available locally, then - from a custom event, once the dialog's
   own event has returned - copies the first number to the clipboard and
   executes the McMaster command. Fusion runs one command at a time, so that
   closes the generator dialog; its state is kept for step 4.
2. The user pastes the number (Ctrl+V, Enter), picks 3-D STEP and clicks
   Save. Fusion inserts the part and immediately opens its Initial Position
   move dialog, which this module dismisses (by starting the Select command,
   the same as pressing Esc) once the requested part is in the design - its
   position is irrelevant, the part is deleted after harvesting.

   Pre-filling McMaster's search was investigated and rejected: the command
   takes no input (CommandDefinition.execute ignores its argument), no text
   command addresses its browser, and the window is an anonymous Qt window
   ('Fusion360') that often is not even in the foreground - keystrokes sent
   to it could land in Fusion as shortcuts, or in another application.
3. A worker thread polls (via the same custom event - the API is only touched
   on the main thread) until Fusion has been idle for a moment after the
   insert. Nothing is touched while the McMaster or move dialog is open:
   acting mid-command is how Fusion gets crashed. Then the part is
   *harvested*: the new component whose name starts with the part number
   ("6389K723_Dry-Running Nylon Sleeve Bearing") is exported as STEP into the
   mcmaster cache, measured exactly like a downloaded STEP, and deleted - the
   generator places its own copies at Generate/Update.
4. The next queued part repeats 1-3; after the last, the generator dialog is
   reopened with everything the user had entered (``on_done``), where the
   newly cached parts autofill like any other.

Needs the add-in (it must stay loaded between steps); ``install`` is called
from the add-in's run(), ``uninstall`` from stop().
"""

import json
import os
import subprocess
import sys
import threading
import time
import traceback

import adsk.core
import adsk.fusion

from . import mcmaster

MCM_COMMAND_ID = 'InsertMcMasterCarrComponentCommand'
EVENT_ID = 'ssm.mcmasterInsert'
IDLE_COMMAND_ID = 'SelectCommand'
MOVE_COMMAND_ID = 'FusionMoveCommand'   # Fusion's Initial Position dialog

POLL_S = 0.5
IDLE_POLLS = 3            # Fusion must stay idle this long (1.5 s) before harvest
TIMEOUT_S = 15 * 60       # give up waiting for the user after this

_app = None
_ui = None
_event = None
_handlers = []
_state = None             # the insert session in progress, or None


def _log(message):
    try:
        _app.log('ssm McMaster insert: %s' % message)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Lifecycle

def install(app, ui):
    """Register the custom event. Call from the add-in's run()."""
    global _app, _ui, _event
    _app, _ui = app, ui
    try:
        app.unregisterCustomEvent(EVENT_ID)
    except Exception:
        pass
    _event = app.registerCustomEvent(EVENT_ID)
    handler = _EventHandler()
    _event.add(handler)
    _handlers.append(handler)


def uninstall():
    global _event, _state
    _state = None
    try:
        if _event is not None:
            for handler in _handlers:
                _event.remove(handler)
        if _app is not None:
            _app.unregisterCustomEvent(EVENT_ID)
    except Exception:
        pass
    _event = None
    del _handlers[:]


def available():
    """True when the add-in is installed and Fusion has the McMaster command."""
    return (_event is not None and _ui is not None
            and _ui.commandDefinitions.itemById(MCM_COMMAND_ID) is not None)


def busy():
    return _state is not None


# ---------------------------------------------------------------------------
# Session

def begin(requests, on_done):
    """Start inserting `requests` - [(kind, part_number), ...].

    on_done(notes) runs on the main thread when the session ends (finished,
    cancelled or timed out), with readable notes for the dialog's status.
    """
    global _state
    _state = {'queue': list(requests), 'current': None, 'before': set(),
              'notes': [], 'on_done': on_done, 'stop': threading.Event()}
    _fire('launch')


def _fire(action):
    _app.fireCustomEvent(EVENT_ID, json.dumps({'action': action}))


class _EventHandler(adsk.core.CustomEventHandler):
    def notify(self, args):
        try:
            action = json.loads(args.additionalInfo or '{}').get('action')
            if _state is None:
                return
            if action == 'launch':
                _launch()
            elif action == 'poll':
                _poll()
        except Exception:
            _log('internal error\n' + traceback.format_exc())
            _finish('Internal error during the McMaster insert - details in '
                    'the Text Commands palette.')


def _occurrence_tokens(design):
    return {occ.entityToken for occ in design.rootComponent.allOccurrences}


def _launch():
    state = _state
    if not state['queue']:
        _finish()
        return
    kind, part_number = state['queue'].pop(0)
    design = adsk.fusion.Design.cast(_app.activeProduct)
    definition = _ui.commandDefinitions.itemById(MCM_COMMAND_ID)
    if design is None or definition is None:
        _finish('Fusion\'s Insert McMaster-Carr Component is not available here.')
        return
    state.update(current=(kind, part_number), before=_occurrence_tokens(design),
                 started=time.time(), seen_busy=False, idle=0,
                 seen_mcmaster=False, dismissed=False)
    copy_to_clipboard(part_number)
    _log('opening McMaster for %s %s (part number copied to the clipboard)'
         % (kind, part_number))
    definition.execute()
    state['stop'] = threading.Event()
    threading.Thread(target=_poller, args=(state['stop'],), daemon=True).start()


def _poller(stop):
    """Worker thread: only fires events; never touches the API itself."""
    while not stop.wait(POLL_S):
        try:
            _fire('poll')
        except Exception:
            return


def _poll():
    state = _state
    active = _ui.activeCommand
    if active == MCM_COMMAND_ID:
        state['seen_mcmaster'] = True
    elif active == MOVE_COMMAND_ID and state['seen_mcmaster'] \
            and not state['dismissed'] and _requested_part_inserted():
        # Initial Position for the part we asked for: dismiss it so the user
        # only has to click Save. The insert itself is already committed.
        state['dismissed'] = True
        _log('dismissing Initial Position for %s' % state['current'][1])
        _ui.commandDefinitions.itemById(IDLE_COMMAND_ID).execute()
    if active != IDLE_COMMAND_ID:
        # The McMaster dialog, then Initial Position (or anything the user
        # does meanwhile): wait.
        state['seen_busy'] = True
        state['idle'] = 0
    elif state['seen_busy']:
        state['idle'] += 1
    if time.time() - state['started'] > TIMEOUT_S:
        state['stop'].set()
        kind, part_number = state['current']
        state['notes'].append('Gave up waiting for the McMaster insert of %s.'
                              % part_number)
        _finish()
        return
    if state['idle'] < IDLE_POLLS:
        return
    state['stop'].set()
    note = _harvest()
    if note:
        state['notes'].append(note)
    _fire('launch')   # next part, or finish


def _requested_part_inserted():
    design = adsk.fusion.Design.cast(_app.activeProduct)
    part_number = _state['current'][1]
    return any(mcmaster.names_part(occ.component.name, part_number)
               for occ in design.rootComponent.allOccurrences
               if occ.entityToken not in _state['before'])


def _harvest():
    """Turn the inserted component into a cache entry. Returns a note."""
    kind, part_number = _state['current']
    design = adsk.fusion.Design.cast(_app.activeProduct)
    new = [occ for occ in design.rootComponent.allOccurrences
           if occ.entityToken not in _state['before']]
    if not new:
        return 'No part was inserted for %s (McMaster dialog cancelled?).' % part_number
    matching = [occ for occ in new
                if mcmaster.names_part(occ.component.name, part_number)]
    if not matching:
        return ('Inserted %s, which is not %s - left it in the design untouched.'
                % (', '.join(occ.component.name for occ in new), part_number))
    component = matching[0].component
    dims = harvest_component(design, component, part_number, kind)
    for occ in matching:
        try:
            occ.deleteMe()
        except Exception:
            pass
    return '%s %s: inserted through Fusion and cached (%s).' % (
        kind.capitalize(), part_number, dims.get('description') or 'part')


def harvest_component(design, component, part_number, kind):
    """Export `component` as the cached STEP for `part_number` and cache its
    measured dimensions. Returns the dims. The measurement is the same code a
    hand-downloaded STEP goes through, so both routes agree."""
    part_number = part_number.strip().upper()
    path = os.path.join(mcmaster.cache_dir(), part_number + '.step')
    manager = design.exportManager
    options = manager.createSTEPExportOptions(path, component)
    if not manager.execute(options) or not os.path.isfile(path):
        raise RuntimeError('STEP export of %s failed.' % component.name)
    dims = mcmaster.dims_from_step(path, kind)
    dims['description'] = mcmaster.description_from_name(component.name)
    mcmaster.save_dims(part_number, dims)
    return dims


def _finish(note=None):
    global _state
    state = _state
    _state = None
    if state is None:
        return
    state['stop'].set()
    notes = state['notes'] + ([note] if note else [])
    try:
        state['on_done'](notes)
    except Exception:
        _log('reopening the dialog failed\n' + traceback.format_exc())


# ---------------------------------------------------------------------------
# Clipboard

def copy_to_clipboard(text):
    """Best effort: the user can always type the number instead."""
    try:
        if sys.platform == 'win32':
            subprocess.run(['clip'], input=text.encode('ascii', 'ignore'),
                           creationflags=0x08000000, timeout=5, check=False)
        elif sys.platform == 'darwin':
            subprocess.run(['pbcopy'], input=text.encode('utf-8'),
                           timeout=5, check=False)
    except Exception:
        pass
