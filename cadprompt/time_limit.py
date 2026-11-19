"""Ends Python code that runs too long, in the thread that runs it.

    with Limit(60, TooLong):
        exec(code)

The model's code runs in FreeCAD's main thread. A loop that does not end would
freeze FreeCAD for good, and the Stop button with it.

A second thread watches the clock and, when the time is up, asks Python to
raise the exception in the thread that runs the code. Python does that between
two instructions, also in a loop that stays on one line. It asks again twice a
second, so code that catches the exception and goes on is stopped again.
One long FreeCAD call is not cut short, and `except:` with nothing after it,
inside the loop, can still keep the loop alive.

A trace function (sys.settrace) is not enough: Python 3.10 and 3.11 call it
for a new line only, and `while True: pass` never reaches one. It is the
fallback for a Python without ctypes. No FreeCAD and no Qt in here.
"""

import sys
import threading
import time

try:
    import ctypes
    _raise_in = ctypes.pythonapi.PyThreadState_SetAsyncExc
except Exception:        # no ctypes in this Python: the trace function does the work
    ctypes = _raise_in = None

AGAIN = 0.5      # seconds until the exception is raised once more


class Limit:
    """Raise `exception` (a class, made without arguments) in this thread after `seconds`."""

    def __init__(self, seconds, exception):
        self.seconds = seconds
        self.exception = exception
        self._watched = _raise_in is not None

    def __enter__(self):
        if not self._watched:
            self._old_trace = sys.gettrace()
            sys.settrace(self._trace_function())
            return self
        self._thread = threading.get_ident()
        self._done = threading.Event()
        self._lock = threading.Lock()
        threading.Thread(target=self._watch, name="cadprompt-time-limit", daemon=True).start()
        return self

    def __exit__(self, *exc):
        if not self._watched:
            sys.settrace(self._old_trace)
            return False
        with self._lock:         # after this the watcher raises nothing more
            self._done.set()
            # one that was asked for but has not arrived yet must not arrive later, in other code
            _raise_in(ctypes.c_ulong(self._thread), None)
        return False

    def _watch(self):
        wait = self.seconds
        while not self._done.wait(wait):
            with self._lock:
                if self._done.is_set():
                    return
                _raise_in(ctypes.c_ulong(self._thread), ctypes.py_object(self.exception))
            wait = AGAIN

    def _trace_function(self):
        end = time.monotonic() + self.seconds
        # Python 3.10 and 3.11 report a loop on one line only when every instruction is traced
        opcodes = sys.version_info[:2] in ((3, 10), (3, 11))
        exception = self.exception

        def trace(frame, event, arg):
            if opcodes:
                frame.f_trace_opcodes = True
            if time.monotonic() > end:
                raise exception()
            return trace
        return trace
