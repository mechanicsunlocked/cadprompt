# RUN: python
"""Tests of cadprompt/time_limit.py with plain Python: no FreeCAD needed.

    python3 tests/test_time_limit.py

Runs with any Python from 3.8 on. tests/test_local_tools.py runs the same
limit inside FreeCAD, with the Python that FreeCAD brings.
"""

import os
import sys
import threading
import time
import unittest

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from utf8_output import utf8_output            # noqa: E402

utf8_output()

from cadprompt import time_limit               # noqa: E402

# Loops that do not end. The second one stays on one line: the trace function
# of Python 3.10 and 3.11 gets no call for it, which froze FreeCAD 1.1.
LOOPS = ("while True:\n    pass",
         "while True: pass",
         "def f():\n    while True:\n        try:\n            1 / 0\n        except Exception:\n            pass\nf()",
         "import time\nwhile True:\n    try:\n        time.sleep(0.01)\n    finally:\n        n = 1")


class TooLong(BaseException):
    pass


def run(code, seconds=0.5):
    """('stopped' or 'ended', seconds it took)."""
    compiled = compile(code, "<test>", "exec")
    start = time.monotonic()
    try:
        with time_limit.Limit(seconds, TooLong):
            exec(compiled, {})
    except TooLong:
        return "stopped", time.monotonic() - start
    return "ended", time.monotonic() - start


def hard_limit(test, seconds):
    def end():
        sys.stdout.write("\nFAIL: %s did not come back in %d seconds\n" % (test.id(), seconds))
        sys.stdout.flush()
        os._exit(3)
    timer = threading.Timer(seconds, end)
    timer.daemon = True
    timer.start()
    test.addCleanup(timer.cancel)


class Both:
    """The same tests for the watcher thread and for the trace function."""

    def setUp(self):
        hard_limit(self, 60)

    def test_loops_that_do_not_end_are_stopped(self):
        for code in LOOPS:
            what, took = run(code)
            self.assertEqual(what, "stopped", code)
            self.assertLess(took, 5, code)
            self.assertIsNone(sys.gettrace(), code)

    def test_code_that_ends_in_time_is_left_alone(self):
        self.assertEqual(run("n = sum(range(1000))", 5)[0], "ended")
        self.assertIsNone(sys.gettrace())

    def test_an_error_of_the_code_comes_through(self):
        with self.assertRaises(ZeroDivisionError):
            with time_limit.Limit(5, TooLong):
                1 / 0
        self.assertIsNone(sys.gettrace())

    def test_nothing_arrives_after_the_end(self):
        for _ in range(20):                      # code that ends just when the time is up
            run("import time\ntime.sleep(0.05)", 0.05)
        end = time.monotonic() + 1.5             # a late exception would end this loop
        while time.monotonic() < end:
            pass


class Watcher(Both, unittest.TestCase):
    def setUp(self):
        Both.setUp(self)
        if time_limit._raise_in is None:
            self.skipTest("this Python has no ctypes")

    def test_the_watcher_thread_ends(self):
        run("n = 1", 30)
        end = time.monotonic() + 5
        while time.monotonic() < end and any(t.name == "cadprompt-time-limit" for t in threading.enumerate()):
            time.sleep(0.01)
        self.assertEqual([t.name for t in threading.enumerate() if t.name == "cadprompt-time-limit"], [])

    def test_code_that_catches_it_and_goes_on_is_stopped_again(self):
        code = ("n = 0\nwhile n < 3:\n    try:\n        while True:\n            pass\n"
                "    except BaseException:\n        n += 1\nwhile True:\n    pass")
        what, took = run(code)
        self.assertEqual(what, "stopped")
        self.assertGreater(took, 0.5 + 2 * time_limit.AGAIN)      # it was raised four times


class TraceFunction(Both, unittest.TestCase):
    """What a Python without ctypes gets."""

    def setUp(self):
        Both.setUp(self)
        real = time_limit._raise_in
        time_limit._raise_in = None
        self.addCleanup(setattr, time_limit, "_raise_in", real)


if __name__ == "__main__":
    unittest.main(verbosity=2)
