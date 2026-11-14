"""A stand-in for cadprompt/local_engine.py, with the same interface, for the panel tests.

The panel tests put this module in sys.modules['cadprompt.local_engine'], so
they test the panel alone: no model server, no FreeCAD tools. It records what
the panel asks for and plays back what the test wants the "model" to do.
"""

from PySide import QtCore

calls = []      # what the panel asked for, in order: ("start", project, prompt, config), ("stop",), ...
replay = []     # what the next start() sends back: ("text", t), ("tool", name, detail),
                # ("tool_error", name), ("finished", dict). No "finished": the run keeps going.
past = {}       # project -> [(kind, text), ...], what history() gives


class LocalRun(QtCore.QObject):
    text = QtCore.Signal(str)
    tool = QtCore.Signal(str, str)
    tool_error = QtCore.Signal(str)
    finished = QtCore.Signal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._on = False
        self._todo = []

    def running(self):
        return self._on

    def start(self, project, prompt, config):
        calls.append(("start", project, prompt, dict(config)))
        self._on = True
        self._todo = list(replay)
        QtCore.QTimer.singleShot(0, self._play)     # like the real one: start() returns at once

    def _play(self):
        for step in self._todo:
            if not self._on:
                return
            if step[0] == "finished":
                self._on = False
            getattr(self, step[0]).emit(*step[1:])

    def stop(self):
        calls.append(("stop",))
        if self._on:
            self._on = False
            self.finished.emit({"is_error": False, "result": "", "stopped": True})


def history(project, limit=200):
    calls.append(("history", project))
    return list(past.get(project, []))[-limit:]


def clear(project):
    calls.append(("clear", project))
    past.pop(project, None)
