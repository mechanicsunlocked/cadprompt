"""Work for FreeCAD's main thread that another thread asks for.

FreeCAD is not thread-safe: a tool must run in the GUI thread. A thread that
needs one (the worker of the built-in engine, a request of the MCP server)
makes a Call, hands it over with a queued Qt signal and waits here. The main
thread fills `result` and sets `done`. No Qt in this file: the signal belongs
to the object that owns the work (local_engine._Worker, bridge._Host).

quiet_pipe() is for the same threads: they write to network connections.
"""

import signal
import threading


class Call:
    """One request to the main thread. The asking thread waits on `done`."""

    def __init__(self, name, args):
        self.name, self.args, self.result = name, args, ""
        self.done = threading.Event()


def wait(call, gone, step=0.05):
    """Wait in the asking thread until the main thread has answered `call`.

    gone() is looked at again and again: True means that nobody will ever
    answer (the run was stopped, the panel is closed, the server is down).
    Returns False then, so the wait cannot hang for ever.
    """
    while not call.done.wait(step):
        if gone():
            return False
    return True


def quiet_pipe():
    """Call it in a thread of ours before it writes to a network connection.

    Linux and macOS send the signal SIGPIPE to a program that writes to a
    connection which the other side closed, and that signal ends the program.
    Python itself ignores it, FreeCAD does not: a model server that closes
    the line, or Claude Code that is stopped in the middle of a tool call,
    would take FreeCAD down. With the signal blocked in this thread the write
    gives an error instead. Nothing changes for FreeCAD's own threads.
    """
    if threading.current_thread() is threading.main_thread():
        return                   # not ours to change
    try:
        signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGPIPE})
    except (AttributeError, ValueError, OSError):
        pass                     # Windows has no such signal
