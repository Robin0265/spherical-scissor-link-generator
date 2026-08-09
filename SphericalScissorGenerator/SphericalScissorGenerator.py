# Spherical Scissor Link Generator - Script entry point
#
# Generates the fully-parametric sketch skeleton of an n-rhombi spherical
# scissor (SSM) linkage, after Castro et al., "A compact 3-DOF shoulder
# mechanism constructed with scissors linkages for exoskeleton applications",
# Mechanism and Machine Theory, 2019.
#
# This file only launches the dialog. The dialog itself lives in ssm/command.py
# and is shared with the add-in (../SphericalScissorAddIn), so both entry points
# behave identically. The geometry lives in the rest of the ssm package.
#
# Prefer the add-in for day-to-day use: it adds a toolbar button and is the only
# form that can register a CustomFeature definition at startup. This script
# stays because it needs no install and is convenient for development.

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

from ssm import command  # noqa: E402


def run(context):
    ui = None
    try:
        app = adsk.core.Application.get()
        ui = app.userInterface

        if command.check_design(app, ui) is None:
            return

        definition = command.register(app, ui, terminate_on_destroy=True)
        definition.execute()
        adsk.autoTerminate(False)
    except Exception:
        if ui:
            ui.messageBox('Failed:\n{}'.format(traceback.format_exc()))
