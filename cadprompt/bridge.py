"""The bridge between Claude Code and this FreeCAD: an MCP server in this process.

Claude Code needs nothing but itself: the project's .mcp.json points it at
http://127.0.0.1:<port>/mcp (mcp_server.py), and the panel hands it the
address and the token in its environment. Every tool call arrives in a thread
of the server and is handed to FreeCAD's main thread, the same way the
built-in engine does it (main_thread.py).

This module owns the life of that server. It starts only when the panel is
about to start Claude Code (ensure), never when FreeCAD starts. It stops when
the panel says so (the panel is closed, another source is chosen) and when
FreeCAD quits.

The panel imports this module when it opens. The server and the tools are
imported in ensure(), so a panel that never starts Claude Code loads neither.
"""

import traceback

import FreeCAD as App
from PySide import QtCore

from . import main_thread

INSTRUCTIONS = ("The tools work in the user's open FreeCAD. run_python: App, Part, Sketcher, Draft, math and "
                "Gui are imported, doc is the active document, PROJECT is the project folder, variables stay "
                "between calls, each call is one undo step, and a call that fails is taken back. Check with "
                "get_document_state. For a drawing call make_drawing first.")
GONE = "ERROR: this request was stopped. Nothing was done."

_host = None          # made with the first server, lives in the main thread
_server = None


class _Host(QtCore.QObject):
    """Runs the tools in the main thread for the requests of the server."""

    need_tool = QtCore.Signal(object)       # a main_thread.Call, sent from a thread of the server

    def __init__(self):
        super().__init__()
        self.project, self.source, self.note = "", "account", None
        self._serial = 0          # counts requests of the user; a call of an earlier one is not run
        self._backed = set()      # model files that have their backup copy for this request
        self._in_tool = False
        self.need_tool.connect(self._on_tool, QtCore.Qt.QueuedConnection)

    def begin(self, project, source, note):
        """A new request of the user starts: Claude Code is about to run."""
        self._serial += 1
        from . import local_tools
        self.project, self.source, self.note = project or "", source or "account", note
        self._backed = set()
        local_tools.begin_request()

    def end(self):
        """Claude Code is not running any more: a call that still waits must not run."""
        self._serial += 1

    # ---- in a thread of the server ----------------------------------------------
    def call(self, name, args):
        from . import mcp_server
        server, call = _server, main_thread.Call(name, args)
        call.serial = self._serial
        self.need_tool.emit(call)
        # the wait ends when the server is stopped, so a request cannot hang on a closed panel
        if not main_thread.wait(call, lambda: server is None or not server.running()):
            return [mcp_server.text_item(GONE)], True
        return call.result

    # ---- in the main thread -----------------------------------------------------
    def _on_tool(self, call):
        from . import mcp_server
        if call.serial != self._serial or not running():
            call.result = ([mcp_server.text_item(GONE)], True)
            call.done.set()
            return
        if self._in_tool:
            # a tool that opened a dialog lets events through; never run two tools at once
            QtCore.QTimer.singleShot(50, lambda: self._on_tool(call))
            return
        self._in_tool = True
        try:
            call.result = self._run(call.name, call.args)
        except Exception:
            call.result = ([mcp_server.text_item("ERROR in tool:\n" + traceback.format_exc(limit=3))], True)
        finally:
            self._in_tool = False
            call.done.set()

    def _run(self, name, args):
        from . import approval, local_agent, local_prompt, local_tools, mcp_server
        if name in local_prompt.CHANGING:
            # the user decides, in this thread, before anything of the step happens
            if not approval.request(name, args, self.source):
                return [mcp_server.text_item(approval.DECLINED)], False
            self._backup()
        if name == "get_view":
            try:
                return [mcp_server.image_item(local_tools.get_view(), "image/png")], False
            except local_tools.NoView as exc:
                return [mcp_server.text_item("ERROR: %s" % exc)], True
        text = local_tools.run_tool(self.project, name, args)
        return [mcp_server.text_item(text)], bool(local_agent.find_problem(name, text))

    def _backup(self):
        """Before the first change of a request, keep the model file as it is on disk."""
        doc = App.ActiveDocument
        path = doc.FileName if doc is not None else ""
        if not path or not self.project or path in self._backed:
            return
        self._backed.add(path)
        from . import projects
        try:
            copy = projects.backup(self.project, path)
        except OSError as exc:
            App.Console.PrintWarning(f"CADprompt: no backup copy of the model could be made: {exc}\n")
            return
        if copy and self.note:
            self.note("backup", "model/.backup/" + copy.replace("\\", "/").rsplit("/", 1)[-1])


def ensure(project="", source="account", note=None):
    """Make sure that the server runs, for a request in `project`. Returns (url, token).

    Call it in the main thread, right before Claude Code starts. `source` is
    the AI source for approval.request; note(name, detail) is told what the
    bridge did by itself (the backup copy). Raises OSError when the server
    cannot listen.
    """
    global _host, _server
    from . import local_prompt, mcp_server
    if _host is None:
        _host = _Host()
        app = QtCore.QCoreApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(stop)
    if _server is None:
        server = mcp_server.Server(local_prompt.bridge_tools(), _host.call, name="cadprompt",
                                   version="1", instructions=INSTRUCTIONS)
        server.start()
        _server = server
    _host.begin(project, source, note)
    return _server.url, _server.token


def idle():
    """Claude Code ended or was stopped: its calls that did not start yet are dropped."""
    if _host is not None:
        _host.end()


def stop():
    """Close the server. The next ensure() starts a new one, with a new port and token."""
    global _server
    server, _server = _server, None
    if _host is not None:
        _host.end()
    if server is not None:
        server.stop()


def running():
    return _server is not None and _server.running()
