"""Spherical scissor (SSM) linkage generator.

The entry script owns only the Fusion command dialog; everything else lives
here:

    vectors     small 3D helpers that do not touch the document
    parameters  the parameter table, expression solving, and validation
    frame       resolving the sphere frame from the user's plane selections
    sketching   low-level sketch primitives (nudged arcs, spokes, chords, dims)
    builder     the link chain itself
    audit       independent re-measurement of what was built
"""

PREFIX = 'SSM_'

__all__ = ['PREFIX', 'vectors', 'parameters', 'frame', 'sketching', 'builder', 'audit']
