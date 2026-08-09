# Spherical Scissor Link Generator - Add-In entry point
#
# Adds a "Spherical Scissor" panel with a Generate button to the UTILITIES tab,
# and registers the CustomFeature definition at startup.
#
# Registering that definition in run() is not a stylistic choice: Autodesk's
# documentation states the CustomFeatureDefinition must be created when the
# add-in starts. A Script cannot satisfy that - it runs once and terminates -
# which is why every attempt to create a custom feature from the script failed
# with "make params invalid". The definition also has to exist every time a
# document is opened, or Fusion cannot recompute features of that type.
#
# The dialog itself is shared with the script entry point (see ssm/command.py).

import os
import sys
import traceback

import adsk.core
import adsk.fusion

_HERE = os.path.dirname(os.path.abspath(__file__))
# The ssm package lives beside the script entry point; share it rather than
# keeping two copies that can drift apart.
_PACKAGE_ROOT = os.path.join(os.path.dirname(_HERE), 'SphericalScissorGenerator')
for _path in (_PACKAGE_ROOT, _HERE):
    if _path not in sys.path:
        sys.path.insert(0, _path)

for _stale in [name for name in list(sys.modules)
               if name == 'ssm' or name.startswith('ssm.')]:
    del sys.modules[_stale]

from ssm import command, custom_feature  # noqa: E402

PANEL_ID = 'SSMPanel'
PANEL_NAME = 'Spherical Scissor'
TAB_ID = 'ToolsTab'          # the UTILITIES tab
CONTROL_ID = 'SSMGenerateControl'

_app = None
_ui = None


def _utilities_tab():
    workspace = _ui.workspaces.itemById('FusionSolidEnvironment')
    tab = workspace.toolbarTabs.itemById(TAB_ID)
    if tab is None:
        # Fall back to whichever tab exists rather than failing to load.
        tab = workspace.toolbarTabs.item(0)
    return tab


def _panel():
    tab = _utilities_tab()
    panel = tab.toolbarPanels.itemById(PANEL_ID)
    if panel is None:
        panel = tab.toolbarPanels.add(PANEL_ID, PANEL_NAME)
    return panel


def run(context):
    global _app, _ui
    try:
        _app = adsk.core.Application.get()
        _ui = _app.userInterface

        # Must happen at add-in load, before any custom feature is created or
        # any document containing one is opened. The icon folder doubles as the
        # timeline node's icon for features of this type.
        custom_feature.register_definition(_app, command.resource_folder() or _HERE)

        definition = command.register(_app, _ui, terminate_on_destroy=False)

        # Double-clicking the feature in the timeline reopens this dialog.
        custom_feature.set_edit_command(command.CMD_ID)

        panel = _panel()
        if panel.controls.itemById(CONTROL_ID) is None:
            control = panel.controls.addCommand(definition, CONTROL_ID)
            control.isPromoted = True
            control.isPromotedByDefault = True
    except Exception:
        if _ui:
            _ui.messageBox('Spherical Scissor add-in failed to load:\n{}'
                           .format(traceback.format_exc()))


def stop(context):
    try:
        app = adsk.core.Application.get()
        ui = app.userInterface

        tab = ui.workspaces.itemById('FusionSolidEnvironment').toolbarTabs.itemById(TAB_ID)
        panel = tab.toolbarPanels.itemById(PANEL_ID) if tab else None
        if panel:
            control = panel.controls.itemById(CONTROL_ID)
            if control:
                control.deleteMe()
            if panel.controls.count == 0:
                panel.deleteMe()

        command.unregister(ui)
        custom_feature.forget_definition()
    except Exception:
        # A failure while unloading must not block Fusion from unloading.
        pass
