"""What a test prints is UTF-8, whatever the encoding of the system is.

FreeCAD on macOS gives Python an ASCII stdout and Windows gives a code page.
A check name with "…" or "·" in it would then end the test with a
UnicodeEncodeError. Helper for the tests; standard library only.
"""

import sys


def utf8_output():
    for stream in (sys.stdout, sys.stderr, sys.__stdout__, sys.__stderr__):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass             # not a text file of Python: FreeCAD's report view, or no output at all
