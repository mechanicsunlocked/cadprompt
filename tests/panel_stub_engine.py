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
images = []     # the images of every start(), in order: a list of project paths each
MAX_IMAGES = 4


class LocalRun(QtCore.QObject):
    text = QtCore.Signal(str)
    tool = QtCore.Signal(str, str)
    tool_error = QtCore.Signal(str)
    tool_done = QtCore.Signal(str)
    finished = QtCore.Signal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._on = False
        self._todo = []

    def running(self):
        return self._on

    def start(self, project, prompt, config, images=None):
        calls.append(("start", project, prompt, dict(config)))
        globals()["images"].append(list(images or []))
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


class ImageError(ValueError):
    def __init__(self, text, path=""):
        super().__init__(text)
        self.path = path


def images_note(n):
    return "with %d image%s" % (n, "" if n == 1 else "s")


def project_images(project, files):
    """Like the real one, without Qt: a file is an image when it starts like a PNG."""
    import os
    out = []
    for f in files or []:
        with open(f, "rb") as fh:
            if fh.read(4) != b"\x89PNG":
                raise ImageError("`%s` is not an image that CADprompt can read (PNG or JPEG, for "
                                 "example). It was not sent." % os.path.basename(f), f)
        out.append(os.path.relpath(f, project).replace(os.sep, "/"))
    return out
