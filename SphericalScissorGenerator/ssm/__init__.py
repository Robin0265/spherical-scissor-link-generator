"""Spherical scissor (SSM) linkage generator.

The entry script owns only the Fusion command dialog; everything else lives
here:

    vectors     small 3D helpers that do not touch the document
    parameters  the parameter table, expression solving, and validation
    frame       resolving the sphere frame from the user's plane selections
    sketching   low-level sketch primitives (nudged arcs, spokes, chords, dims)
    builder     the link chain itself
    solids      physical link bodies (bars, bosses, bores) on the skeleton
    draft       custom-graphics draft skeleton for the dialog's live preview
    progress    progress dialog + UI pumping for the long build
    audit       independent re-measurement of what was built
"""

PREFIX = 'SSM_'

# Browser name of the sub-component the mechanism is packed into.
COMPONENT_NAME = 'Spherical Scissor'

# Bumped on every behavioural change. Logged with the stage timings so a
# stale module (the add-in only reloads ssm at Stop/Run) is always visible.
VERSION = '2026.08.24.4-scoped-audit'

__all__ = ['PREFIX', 'COMPONENT_NAME', 'vectors', 'parameters', 'frame',
           'sketching', 'builder', 'solids', 'draft', 'progress', 'audit']
