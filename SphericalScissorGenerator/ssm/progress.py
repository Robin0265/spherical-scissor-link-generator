"""Progress reporting that keeps Fusion painted during a long build.

Multithreading cannot do this job: the Fusion API is single-threaded - every
API call must be made from the UI thread, and the modelling kernel runs its
solves there too, so a worker thread could not build features in the
background even in principle (background threads may only fire custom events,
whose handlers execute back on the UI thread). What a long build CAN do is
yield: a progress dialog plus adsk.doEvents() between features lets Fusion
repaint, report which step it is on, and offer Cancel, instead of appearing
hung until the build finishes.

The Reporter also doubles as the build's profiler: every step() boundary is
timestamped, so after a run report() says how long each stage took, split
into feature-creation time and UI-repaint time. That is the evidence for
where optimisation effort should go - the arithmetic in this package is
microseconds, so the stages are effectively pure Fusion kernel + API time.
"""

import time

import adsk.core


class Cancelled(RuntimeError):
    """Raised by Reporter.step when the user presses Cancel.

    A RuntimeError so the dialog's normal cannot-build reporting shows it as
    a readable message rather than a traceback.
    """

    def __init__(self):
        super(Cancelled, self).__init__(
            'Build cancelled.\nFeatures created so far were kept; re-running '
            'the generator with "Delete features from previous runs" ticked '
            'replaces them.')


def _category(label):
    """Coarse stage group for the timing summary."""
    if label.startswith(('Bar ', 'Boss ')) or label == 'Solids audited':
        return 'solid stage'
    if label.startswith('Link ') or label in ('Spine sketch',
                                              'Skeleton audited'):
        return 'skeleton'
    if label.startswith('Packaging'):
        return 'packaging'
    return 'setup (purge + parameters)'


class Reporter(object):
    """A begin/step/end progress dialog. step() pumps the UI event loop.

    Timing: each step's duration is measured from the end of the previous
    step's UI pump, so stage times exclude repaint time; the pump total is
    reported separately.
    """

    def __init__(self, ui, title):
        self._ui = ui
        self._title = title
        self._dialog = None
        self._value = 0
        self._timeline = []   # (label, seconds), in build order
        self._pump = 0.0      # total seconds spent in adsk.doEvents()
        self._last = None

    def begin(self, total):
        self._value = 0
        self._timeline = []
        self._pump = 0.0
        self._last = time.perf_counter()
        self._dialog = self._ui.createProgressDialog()
        self._dialog.isCancelButtonShown = True
        self._dialog.show(self._title, 'Preparing...', 0, max(1, int(total)), 0)

    def step(self, message):
        if self._dialog is None:
            return
        now = time.perf_counter()
        self._timeline.append((message, now - self._last))
        self._last = now
        if self._dialog.wasCancelled:
            self.end()
            raise Cancelled()
        self._value += 1
        if self._value <= self._dialog.maximumValue:
            self._dialog.progressValue = self._value
        self._dialog.message = message
        adsk.doEvents()
        after = time.perf_counter()
        self._pump += after - now
        self._last = after

    def end(self):
        """Hide the dialog. Safe to call twice, or without begin()."""
        if self._dialog is not None:
            if self._last is not None:
                # Whatever ran since the last step: custom-feature wrap,
                # timeline group, or the stretch up to a cancel.
                self._timeline.append(
                    ('Packaging / wrap-up', time.perf_counter() - self._last))
                self._last = None
            try:
                self._dialog.hide()
            except Exception:
                pass
            self._dialog = None

    def report(self):
        """Multi-line stage-timing summary, or '' when nothing was timed."""
        if not self._timeline:
            return ''
        feature_total = sum(duration for _label, duration in self._timeline)
        total = feature_total + self._pump
        if total <= 0:
            return ''
        groups = {}
        order = []
        for label, duration in self._timeline:
            key = _category(label)
            if key not in groups:
                groups[key] = 0.0
                order.append(key)
            groups[key] += duration

        def row(name, seconds):
            return '    %-28s %7.2f s  (%4.1f%%)' % (name, seconds,
                                                     100.0 * seconds / total)

        lines = ['%s: %.2f s total' % (self._title, total), '  by stage:']
        lines.extend(row(key, groups[key]) for key in order)
        lines.append(row('UI repaint between features', self._pump))
        lines.append('  slowest steps:')
        for label, duration in sorted(self._timeline,
                                      key=lambda pair: -pair[1])[:5]:
            lines.append('    %-28s %7.2f s' % (label, duration))
        return '\n'.join(lines)


def tick(reporter, message):
    """reporter.step when a Reporter is present, silent no-op otherwise, so
    builder and solids behave identically under the self-test."""
    if reporter is not None:
        reporter.step(message)
