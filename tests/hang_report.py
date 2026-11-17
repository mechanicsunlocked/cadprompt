"""When a window test hangs on macOS, write the native call stacks of FreeCAD to a file.

faulthandler shows the Python lines only: a main thread that waits inside Qt
or macOS code shows as one line of the test. `sample` is a part of macOS and
shows the rest. On other systems this does nothing. Helper for the tests;
standard library only.
"""

import os
import subprocess
import sys
import threading

SAMPLE = "/usr/bin/sample"


def watch(seconds, folder, say=None):
    """After `seconds`, if the test still runs, write <folder>/native-stacks.txt."""
    if sys.platform != "darwin" or not os.path.exists(SAMPLE):
        return None

    def report():
        out = os.path.join(folder, "native-stacks.txt")
        try:
            # a plain environment: FreeCAD's own start script puts its library paths into ours
            subprocess.run([SAMPLE, str(os.getpid()), "3", "-file", out], timeout=60,
                           capture_output=True, env={"PATH": "/usr/bin:/bin"})
            if say:
                with open(out, encoding="utf-8", errors="replace") as f:
                    say("The test runs for %d s. Native call stacks (first lines; all in %s):\n%s"
                        % (seconds, os.path.basename(out), "".join(f.readlines()[:70])))
        except Exception as exc:
            if say:
                say("no native call stacks: %s" % exc)

    timer = threading.Timer(seconds, report)
    timer.daemon = True
    timer.start()
    return timer
