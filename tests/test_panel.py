# RUN: freecad-gui
"""Tests for the panel and the settings dialog with all four AI sources.

Run in the real FreeCAD GUI without a display, with a throw-away profile so no
real setting is touched:

    FREECAD_USER_HOME=$(mktemp -d) QT_QPA_PLATFORM=offscreen timeout 300 freecad tests/test_panel.py

Nothing real is called: the local engine is panel_stub_engine.py, `claude` is
panel_fake_claude.py and every key is made up. The bridge is a stand-in that
counts, until the last part: there the pretend claude calls the real one.
"""

import faulthandler
import http.server
import json
import os
import re
import shutil
import stat
import sys
import threading
import time
import traceback

sys.dont_write_bytecode = True       # no __pycache__ in the repository
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.dirname(HERE), HERE]

from utf8_output import utf8_output            # noqa: E402

utf8_output()

OUT = sys.__stdout__             # FreeCAD's own sys.stdout goes to its report view
LOG = os.path.join(os.environ.get("FREECAD_USER_HOME") or ".", "test_panel.log")
FAILED = []
COUNT = [0]
SLOT_ERRORS = []                 # exceptions inside Qt slots do not reach us any other way


def say(text):
    try:                         # FreeCAD's window program can be without any output (Windows)
        OUT.write(text + "\n")
        OUT.flush()
    except (OSError, ValueError, AttributeError):
        pass
    if os.environ.get("FREECAD_USER_HOME"):
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(text + "\n")


def check(name, ok, detail=""):
    COUNT[0] += 1
    if not ok:
        FAILED.append(name)
    say(("ok    " if ok else "FAIL  ") + name + (f"   [{detail}]" if detail and not ok else ""))


class _Fake(http.server.BaseHTTPRequestHandler):
    """A pretend model server for 'Find models'."""
    seen = []                    # (port, path, Authorization header) of every request
    keys = []                    # the x-api-key header of the requests to /anthropic

    def log_message(self, *a):
        pass

    def _json(self, body):
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        _Fake.seen.append((self.server.server_address[1], self.path, self.headers.get("Authorization")))
        try:
            if self.path.startswith("/anthropic/v1/models"):
                _Fake.keys.append(self.headers.get("x-api-key"))
                if (self.headers.get("x-api-key") or "").startswith("sk-ant-"):
                    self._json({"data": [{"type": "model", "id": "claude-fake-1"},
                                         {"type": "model", "id": "claude-fake-2"}], "has_more": False})
                else:
                    self.send_response(401)
                    self.end_headers()
            elif self.path == "/v1/models":
                self._json({"data": [{"id": "google/gemma-4-e4b"}, {"id": "other-model"}]})
            elif self.path == "/slow/models":
                time.sleep(2)
                self._json({"data": [{"id": "slow-model"}]})
            elif self.path == "/locked/models":
                self.send_response(401)
                self.end_headers()
            elif self.path == "/drip/models":      # one byte a second: the socket timeout never trips
                self.send_response(200)
                self.end_headers()
                for _ in range(12):
                    self.wfile.write(b" ")
                    self.wfile.flush()
                    time.sleep(1)
            else:
                self._json({"error": "Unexpected endpoint or method."})
        except OSError:
            pass


def main():
    import FreeCAD as App
    import FreeCADGui as Gui
    from PySide import QtCore, QtGui, QtWidgets

    # Without a display Qt warns "This plugin does not support raise()". FreeCAD 1.1 sends that
    # warning to its notification area while the area is busy showing itself, and locks up.
    QtCore.QLoggingCategory.setFilterRules("default.warning=false")

    def spin(ms=50):
        # No event loop of our own: on macOS a QEventLoop that is started here never
        # came back once a document had its 3D view (the bridge had answered and the
        # object was made). processEvents() is what the other window tests use.
        end = time.monotonic() + ms / 1000.0
        while True:
            QtWidgets.QApplication.processEvents()
            if time.monotonic() >= end:
                return
            time.sleep(0.002)

    def wait_for(cond, seconds=10):
        end = time.time() + seconds
        while time.time() < end:
            if cond():
                return True
            spin(20)
        return bool(cond())

    home = os.environ["FREECAD_USER_HOME"]
    say("FreeCAD user data: " + App.getUserAppDataDir())

    # ---- a pretend claude, the stub engine, a pretend bridge ----
    # In a folder with a space and a letter that is not ASCII, as in C:\\Users\\Zoë Müller.
    bindir = os.path.join(home, "bin of Zoë")
    try:
        os.makedirs(bindir)
    except (OSError, ValueError) as exc:
        check("(setup) a folder name with a space and a letter that is not ASCII can be made", False, repr(exc))
        bindir = os.path.join(home, "bin of Zoe")
        os.makedirs(bindir)
    python = os.environ.get("CADPROMPT_TEST_PYTHON") or shutil.which("python3") or shutil.which("python") or "python3"
    script = os.path.join(HERE, "panel_fake_claude.py")
    if os.name == "nt":
        # what `npm install -g` makes on Windows: a .cmd file, which only cmd.exe can run
        fake_claude = os.path.join(bindir, "claude.cmd")
        with open(fake_claude, "w") as f:
            f.write('@echo off\n"%s" "%s" %%*\n' % (python, script))
    else:
        fake_claude = os.path.join(bindir, "claude")
        with open(fake_claude, "w", encoding="utf-8") as f:
            f.write('#!/bin/sh\nexec "%s" "%s" "$@"\n' % (python, script))
        os.chmod(fake_claude, 0o755)
    # What the start script of a FreeCAD bundle (AppImage, macOS app) leaves in the
    # environment. Nothing of it may reach a program that the addon starts.
    bundle = os.path.join(home, "pretend bundle")
    os.makedirs(os.path.join(bundle, "usr", "bin"))
    os.environ["PYTHONPATH"] = os.path.join(bundle, "usr", "lib")
    os.environ["PANEL_BUNDLE_CERT"] = os.path.join(bundle, "usr", "ssl", "cacert.pem")
    os.environ["PANEL_BUNDLE_OTHER"] = "kept"
    os.environ["PATH"] = os.pathsep.join([os.path.join(bundle, "usr", "bin"), os.environ.get("PATH", "")])
    claude_log = os.path.join(home, "fake-claude.jsonl")
    os.environ["PANEL_FAKE_CLAUDE_LOG"] = claude_log
    os.environ["CLAUDE_CONFIG_DIR"] = os.path.join(home, "claude-config")
    # what a user could have in the shell: none of it may reach Claude Code unasked
    os.environ["ANTHROPIC_API_KEY"] = "shell-key-must-not-leak"
    os.environ["ANTHROPIC_AUTH_TOKEN"] = "shell-token-must-not-leak"
    os.environ["ANTHROPIC_BASE_URL"] = "http://shell.invalid"

    def claude_calls():
        try:
            with open(claude_log, encoding="utf-8") as f:
                return [json.loads(line) for line in f]
        except OSError:
            return []

    import panel_stub_engine as stub
    import cadprompt
    sys.modules["cadprompt.local_engine"] = stub
    cadprompt.local_engine = stub
    from cadprompt import backends, bridge, panel, paths, projects, runner, settings_dialog
    from cadprompt.settings_dialog import SettingsDialog

    real_roots = paths.bundle_roots()
    say("FreeCAD's own bundle: " + (", ".join(real_roots) or "none (a FreeCAD of the system)"))
    paths.bundle_roots = lambda: real_roots + [bundle]

    bridge_calls = []
    bridge_ok = [True]
    real_ensure = bridge.ensure

    def fake_ensure(project="", source="account", note=None):
        bridge_calls.append((project, source))
        if not bridge_ok[0]:
            raise RuntimeError("the test turned the bridge off")
        return "http://127.0.0.1:1/mcp", "test-token"
    bridge.ensure = fake_ensure
    asked = []
    QtWidgets.QMessageBox.question = staticmethod(lambda *a, **k: asked.append(a) or QtWidgets.QMessageBox.No)

    p = paths.params()
    p.SetString("ProjectsDir", os.path.join(home, "projects"))
    p.SetString("claudePath", fake_claude)
    project = projects.create("Test part")
    other = projects.create("Other part")
    os.utime(project, None)      # the newest project is the one the panel opens
    check("test setup: two projects from the template", os.path.isfile(os.path.join(project, "CLAUDE.md"))
          and os.path.isdir(other) and paths.find_tool("claude") == fake_claude)

    def kinds(pan, kind):
        return [t for k, t in pan.entries if k == kind]

    def setup_errors(pan):
        return kinds(pan, "setup")

    panels = []

    def new_panel(settle=150):
        pan = panel.Panel()
        panels.append(pan)
        spin(settle)             # check_setup runs from a timer
        return pan

    # =====================================================================
    say("\n-- settings dialog --")
    for b in ("account", "apikey", "local", "custom"):
        p.SetString("Backend", b)
        dlg = SettingsDialog()
        see = lambda w, dlg=dlg: w.isVisibleTo(dlg)
        check(f"dialog opens with {b} chosen", dlg.backend() == b and dlg.radios[b].isChecked())
        check(f"{b}: only its own fields are shown",
              (see(dlg.apikey_box), see(dlg.local_box), see(dlg.custom_box)) ==
              (b == "apikey", b == "local", b == "custom"),
              str((see(dlg.apikey_box), see(dlg.local_box), see(dlg.custom_box))))
        claude_fields = [see(w) for w in (dlg.web, dlg.claude)]
        code = b in ("account", "custom")        # the API key and the local model need no Claude Code
        check(f"{b}: web toggle and claude program " + ("shown" if code else "hidden"),
              claude_fields == [code] * 2, str(claude_fields))
        check(f"{b}: no field for uvx: Claude Code needs no other program",
              not hasattr(dlg, "uvx") and not [w for w in dlg.findChildren(QtWidgets.QLabel) if "uvx" in w.text()])
        check(f"{b}: Claude Code model " + ("shown" if b == "account" else "hidden"),
              see(dlg.model_override) == (b == "account"))
        check(f"{b}: projects folder is always there", see(dlg.projects))
        dlg.deleteLater()

    p.SetString("Backend", "account")
    dlg = SettingsDialog()
    see = lambda w: w.isVisibleTo(dlg)
    labels = [dlg.radios[b].text() for b in ("account", "apikey", "local", "custom")]
    check("the four choices are named in plain words", labels == [
        "Claude account (signed in to Claude Code)", "Anthropic API key",
        "Local model or other server (LM Studio, Ollama, OpenAI-compatible)",
        "Claude Code with another endpoint"], str(labels))
    dlg.local.click()
    check("clicking 'Local model' swaps the fields at once",
          see(dlg.local_box) and not see(dlg.claude_box) and not see(dlg.apikey_box))
    check("local: address is pre-filled with LM Studio's, model is an editable list",
          dlg.local_url.text() == "http://127.0.0.1:1234/v1" and dlg.local_model.isEditable()
          and dlg.local_model.currentText() == "")
    dlg.apikey.click()
    check("clicking 'Anthropic API key' swaps again: a key and a model, nothing of Claude Code",
          see(dlg.apikey_box) and not see(dlg.local_box) and not see(dlg.claude_box)
          and see(dlg.api_model) and see(dlg.api_find))
    items = [dlg.api_model.itemText(i) for i in range(dlg.api_model.count())]
    check("API key: the model is an editable list that starts with the default, without asking Anthropic",
          dlg.api_model.isEditable() and dlg.api_model.currentText() == "claude-opus-5-5"
          and items == list(backends.API_MODELS), f"{dlg.api_model.currentText()} {items}")
    check("API key: the hint says what goes to Anthropic", "go to Anthropic" in dlg.hint.text(), dlg.hint.text())
    dlg.api_model.setEditText("claude-haiku-5-5")
    check("API key field hides what is typed and says nothing is saved yet",
          dlg.api_key.edit.echoMode() == QtWidgets.QLineEdit.Password
          and dlg.api_key.edit.placeholderText() != "saved" and not dlg.api_key.drop.isEnabled())
    dlg.api_key.edit.setText("sk-ant-made-up-1")
    dlg.local.click()
    dlg.local_url.setText("http://127.0.0.1:1/v1")
    dlg.local_model.setEditText("some-model")
    dlg.local_key.edit.setText("made-up-local-key")
    dlg.projects.setText(os.path.join(home, "projects"))
    dlg.web.setChecked(True)
    dlg.model_override.setText("sonnet")
    dlg.url.setText("http://localhost:11434")
    dlg.model.setText("endpoint-model")
    dlg.token.setText("endpoint-token")
    dlg._save()
    check("OK saves the source and the local server", (p.GetString("Backend"), p.GetString("LocalUrl"),
          p.GetString("LocalModel")) == ("local", "http://127.0.0.1:1/v1", "some-model"))
    check("OK saves the model for the API key", p.GetString("ApiModel") == "claude-haiku-5-5"
          and backends.engine_config("apikey")["model"] == "claude-haiku-5-5")
    check("OK saves the old fields too", (p.GetBool("AllowWeb"), p.GetString("ModelOverride"),
          p.GetString("BaseUrl"), p.GetString("Model"), p.GetString("AuthToken"), p.GetString("claudePath"))
          == (True, "sonnet", "http://localhost:11434", "endpoint-model", "endpoint-token", fake_claude))
    key_file = os.path.join(App.getUserAppDataDir(), "cadprompt_api_key")
    check("OK saves both keys in their own files" + (", mode 0600" if os.name == "posix" else ""),
          backends.api_key() == "sk-ant-made-up-1" and backends.local_key() == "made-up-local-key"
          and (os.name != "posix" or stat.S_IMODE(os.stat(key_file).st_mode) == 0o600))
    check("no key went into FreeCAD's parameters",
          not [v for (_k, _n, v) in p.GetContents() if "made-up" in str(v)])
    check("OK closed the dialog as accepted", dlg.result() == QtWidgets.QDialog.Accepted)
    dlg.deleteLater()

    dlg = SettingsDialog()
    check("a saved key is never loaded back: empty field, placeholder 'saved'",
          dlg.api_key.edit.text() == "" and dlg.api_key.edit.placeholderText() == "saved"
          and dlg.local_key.edit.text() == "" and dlg.local_key.edit.placeholderText() == "saved")
    dlg._save()
    check("OK with the key fields left empty keeps the keys",
          backends.api_key() == "sk-ant-made-up-1" and backends.local_key() == "made-up-local-key")
    dlg.deleteLater()
    dlg = SettingsDialog()
    dlg.api_key.edit.setText("sk-ant-made-up-2")
    dlg.local_key.drop.click()
    check("Remove only marks the key: nothing is deleted before OK", backends.local_key() == "made-up-local-key"
          and dlg.local_key.edit.placeholderText() != "saved")
    dlg._save()
    check("OK replaces a key that was typed and deletes a key marked with Remove",
          backends.api_key() == "sk-ant-made-up-2" and backends.local_key() == ""
          and not os.path.exists(os.path.join(App.getUserAppDataDir(), "cadprompt_local_key")))
    dlg.deleteLater()
    dlg = SettingsDialog()
    dlg.api_key.drop.click()
    dlg.reject()
    check("Cancel keeps everything", backends.api_key() == "sk-ant-made-up-2" and p.GetString("Backend") == "local")
    dlg.deleteLater()

    # ---- Find models ----
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Fake)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % server.server_address[1]
    ticks = [0]
    ticker = QtCore.QTimer()
    ticker.timeout.connect(lambda: ticks.__setitem__(0, ticks[0] + 1))
    ticker.start(50)

    def find(dlg, url, seconds=10):
        """Press Find models; (seconds the click took, seconds until the answer, GUI ticks meanwhile)."""
        dlg.local_url.setText(url)
        n, t0 = ticks[0], time.time()
        dlg.find.click()
        click = time.time() - t0
        wait_for(lambda: dlg._asking is None, seconds)
        return click, time.time() - t0, ticks[0] - n

    dlg = SettingsDialog()
    dlg.local_model.setEditText("")
    click, total, _ = find(dlg, base + "/v1")
    items = [dlg.local_model.itemText(i) for i in range(dlg.local_model.count())]
    check("Find models fills the list from the server", items == ["google/gemma-4-e4b", "other-model"], str(items))
    check("Find models: with no model typed, the first one is chosen",
          dlg.local_model.currentText() == "google/gemma-4-e4b")
    check("Find models says how many it found", dlg.find_msg.text().startswith("Found 2 models"),
          dlg.find_msg.text())
    dlg.local_model.setEditText("other-model")
    find(dlg, base + "/v1")
    check("Find models keeps the model that was chosen", dlg.local_model.currentText() == "other-model")
    dlg.local_model.setEditText("typed-by-hand")
    find(dlg, base + "/v1")
    check("Find models keeps a name typed by hand that the server does not list",
          dlg.local_model.currentText() == "typed-by-hand" and dlg.local_model.count() == 2)

    click, total, n = find(dlg, base + "/slow")
    check("slow server (2 s): the click returns at once", click < 0.3, f"{click:.2f} s")
    check("slow server: FreeCAD keeps running while it waits", n >= 20 and 1.8 < total < 4, f"{n} ticks, {total:.1f} s")
    check("slow server: the answer still arrives", dlg.local_model.itemText(0) == "slow-model"
          and dlg.find.isEnabled())

    click, total, n = find(dlg, "http://127.0.0.1:1/v1")
    check("dead port: a short plain message, button usable again",
          dlg.find_msg.text() == "No answer from the server. Is it running, and is the address right?"
          and dlg.find.isEnabled() and total < 6, f"{dlg.find_msg.text()!r} after {total:.1f} s")
    check("dead port: the list is left alone", dlg.local_model.itemText(0) == "slow-model")
    find(dlg, base + "/locked")
    check("refused key: says so", dlg.find_msg.text() == "The server did not accept the key.", dlg.find_msg.text())
    find(dlg, base)
    check("wrong address (no /v1): says to check the address", "/v1" in dlg.find_msg.text()
          and "Traceback" not in dlg.find_msg.text(), dlg.find_msg.text())
    find(dlg, "")
    check("empty address: asks for it, does not start", "Enter the server address" in dlg.find_msg.text())
    click, total, n = find(dlg, base + "/drip", seconds=12)
    check("server that never finishes: given up after about 6 s, FreeCAD kept running",
          5 < total < 8 and n >= 60 and dlg.find.isEnabled() and dlg.find_msg.text().startswith("No answer"),
          f"{total:.1f} s, {n} ticks, {dlg.find_msg.text()!r}")
    n = len(_Fake.seen)
    find(dlg, "127.0.0.1:%d/v1" % server.server_address[1])
    check("address without http://: says what is missing, asks nothing",
          dlg.find_msg.text() == "Start the address with http:// or https://, for example http://127.0.0.1:1234/v1"
          and len(_Fake.seen) == n and dlg._asking is None, dlg.find_msg.text())
    dlg.reject()
    dlg.deleteLater()

    # ---- the saved key is for the saved address only ----
    server_b = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Fake)
    threading.Thread(target=server_b.serve_forever, daemon=True).start()
    port_a, port_b = server.server_address[1], server_b.server_address[1]
    base_b = "http://127.0.0.1:%d" % port_b
    dlg = SettingsDialog()
    dlg.local_url.setText(base + "/v1")
    dlg.local_key.edit.setText("KEY-FOR-A")
    dlg._save()
    dlg.deleteLater()
    dlg = SettingsDialog()
    check("the key of server A is saved", backends.local_config()["key"] == "KEY-FOR-A"
          and dlg.local_key.edit.placeholderText() == "saved" and dlg.local_key.drop.isEnabled())
    find(dlg, base + "/locked")
    check("Find models sends the saved key to its own server, on any path",
          _Fake.seen[-1] == (port_a, "/locked/models", "Bearer KEY-FOR-A"), str(_Fake.seen[-1]))
    dlg.local_url.setText(base_b + "/v1")
    check("another address typed: the key row does not say 'saved' any more",
          dlg.local_key.edit.placeholderText() != "saved" and not dlg.local_key.drop.isEnabled())
    find(dlg, base_b + "/v1")
    check("Find models does not send the saved key to another server",
          _Fake.seen[-1] == (port_b, "/v1/models", None) and dlg.find_msg.text().startswith("Found 2"),
          str(_Fake.seen[-1]))
    dlg.local_url.setText(base + "/v1")
    check("back on the saved address: 'saved' again",
          dlg.local_key.edit.placeholderText() == "saved" and dlg.local_key.drop.isEnabled())
    dlg.local_url.setText(base_b + "/v1")
    dlg._save()
    check("OK with a new address and no new key: the old key does not go to the new server",
          backends.local_config()["url"] == base_b + "/v1" and backends.local_config()["key"] == ""
          and backends.local_key() == "", str(backends.local_config()))
    dlg.deleteLater()
    dlg = SettingsDialog()
    dlg.local_url.setText(base + "/v1")
    dlg.local_key.edit.setText("KEY-FOR-A-2")
    dlg._save()
    dlg.deleteLater()
    check("a new address with a new key keeps that key", backends.local_config()["key"] == "KEY-FOR-A-2")
    dlg = SettingsDialog()
    dlg.local_url.setText(base + "/other/v1")
    dlg._save()
    dlg.deleteLater()
    check("another path on the same server keeps the key", backends.local_config() ==
          {"url": base + "/other/v1", "model": backends.local_config()["model"], "key": "KEY-FOR-A-2"},
          str(backends.local_config()))
    backends.set_local_key("")

    # ---- Find models for the API key: asks Anthropic, only on the click ----
    real_url, backends.ANTHROPIC_URL = backends.ANTHROPIC_URL, base + "/anthropic"
    backends.set_local_key("LOCAL-KEY-made-up")
    n = len(_Fake.seen)
    dlg = SettingsDialog()
    dlg.apikey.click()
    spin(300)
    check("API key: opening Settings and choosing the source sends nothing", len(_Fake.seen) == n)

    def find_api(dlg, seconds=10):
        dlg.api_find.click()
        wait_for(lambda: dlg._asking is None, seconds)

    dlg.api_key.drop.click()
    find_api(dlg)
    check("API key: Find models without a key asks for the key and sends nothing",
          dlg.api_find_msg.text() == "Enter your API key first." and len(_Fake.seen) == n, dlg.api_find_msg.text())
    dlg.api_key.edit.setText("sk-ant-typed-made-up")
    find_api(dlg)
    items = [dlg.api_model.itemText(i) for i in range(dlg.api_model.count())]
    check("API key: Find models fills the list from Anthropic and keeps the chosen model",
          items == ["claude-fake-1", "claude-fake-2"] and dlg.api_model.currentText() == "claude-haiku-5-5"
          and dlg.api_find_msg.text().startswith("Found 2 models") and dlg.api_find.isEnabled(),
          f"{items} {dlg.api_model.currentText()} {dlg.api_find_msg.text()}")
    check("API key: one request, the typed key in x-api-key, no Bearer header, not the local key",
          _Fake.seen[n:] == [(port_a, "/anthropic/v1/models?limit=100", None)]
          and _Fake.keys[-1] == "sk-ant-typed-made-up" and "LOCAL-KEY" not in str(_Fake.seen[n:]),
          f"{_Fake.seen[n:]} {_Fake.keys[-1:]}")
    dlg.reject()
    dlg.deleteLater()
    dlg = SettingsDialog()
    dlg.apikey.click()
    find_api(dlg)
    check("API key: Find models uses the saved key when none is typed", _Fake.keys[-1] == "sk-ant-made-up-2",
          str(_Fake.keys[-1:]))
    backends.set_api_key("not-a-key-made-up")
    find_api(dlg)
    check("API key: a refused key is said in plain words",
          dlg.api_find_msg.text() == "Anthropic did not accept the key.", dlg.api_find_msg.text())
    backends.set_api_key("sk-ant-made-up-2")
    backends.ANTHROPIC_URL = "http://127.0.0.1:1"
    find_api(dlg)
    check("API key: no connection is said in plain words, without 'server'",
          dlg.api_find_msg.text().startswith("No answer from Anthropic."), dlg.api_find_msg.text())
    # a proxy in the settings of the computer that does not answer, and no direct way: the text names the two
    names = ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "no_proxy", "NO_PROXY")
    shell = {k: os.environ.pop(k) for k in names if k in os.environ}
    plain = dlg.api_find_msg.text()
    os.environ["https_proxy"] = "http://127.0.0.1:1"
    backends.ANTHROPIC_URL = "https://127.0.0.1:1"
    find_api(dlg)
    check("API key: no connection on a computer with a proxy says that the proxy and the direct way were tried",
          dlg.api_find_msg.text().startswith("No answer from Anthropic.")
          and dlg.api_find_msg.text().endswith("CADprompt tried the proxy and then a direct connection, and "
                                               "neither got through.") and "proxy" not in plain,
          f"{dlg.api_find_msg.text()!r} / {plain!r}")
    del os.environ["https_proxy"]
    os.environ.update(shell)
    backends.ANTHROPIC_URL = base + "/anthropic"

    # ---- the gate: may_contact() is asked before Find models sends anything ----
    gate, real_gate = [], backends.may_contact
    backends.may_contact = lambda kind, url: gate.append((kind, url)) or False
    n = len(_Fake.seen)
    find_api(dlg)
    dlg.local.click()
    find(dlg, base + "/v1")
    check("may_contact() says no: Find models sends nothing, for both sources, and says so",
          gate == [("anthropic", base + "/anthropic"), ("openai", base + "/v1")] and len(_Fake.seen) == n
          and dlg.find_msg.text().startswith("Nothing was sent") and dlg.api_find_msg.text().startswith("Nothing was sent")
          and dlg._asking is None, f"{gate} {_Fake.seen[n:]} {dlg.find_msg.text()!r}")
    backends.may_contact = lambda kind, url: gate.append((kind, url)) or True
    find(dlg, base + "/v1")
    check("may_contact() says yes: asked first, then one request",
          gate[-1] == ("openai", base + "/v1") and len(_Fake.seen) == n + 1, f"{gate} {_Fake.seen[n:]}")
    backends.may_contact = real_gate
    backends.ANTHROPIC_URL = real_url
    backends.set_local_key("")
    dlg.reject()
    dlg.deleteLater()

    server_b.shutdown()
    server_b.server_close()
    ticker.stop()
    server.shutdown()

    # =====================================================================
    say("\n-- panel: opens with every source --")
    for k in ("LocalUrl", "LocalModel"):
        p.RemString(k)
    p.SetBool("AllowWeb", False)
    for b in ("account", "apikey", "local", "custom"):
        p.SetString("Backend", b)
        before = len(SLOT_ERRORS)
        try:
            pan = new_panel()
            ok = True
        except Exception:
            ok = False
            SLOT_ERRORS.append(traceback.format_exc())
        check(f"Panel() with Backend={b}", ok and len(SLOT_ERRORS) == before, "\n".join(SLOT_ERRORS[before:]))
        if ok:
            check(f"{b}: the active source is shown: '{pan.source.full}'",
                  pan.source.full.startswith(backends.label(b)))
            check(f"{b}: the project and its first note are there",
                  pan.project == project and pan.entries[0] == ("note", "Project **test-part**."))
    p.SetString("Backend", "nonsense")
    pan = new_panel()
    check("an unknown Backend value opens as the Claude account", pan.source.full == "Claude account · sonnet",
          pan.source.full)

    # =====================================================================
    say("\n-- panel: local model --")
    p.SetString("Backend", "local")
    p.SetString("LocalModel", "")
    del stub.calls[:]
    real_find_tool = paths.find_tool
    paths.find_tool = lambda name: None      # no Claude Code and no uv on this machine
    bridge_ok[0] = False                     # and no bridge
    n_bridge, n_claude = len(bridge_calls), len(claude_calls())
    pan = new_panel()
    check("local without a model: one setup message, it points to Settings",
          len(setup_errors(pan)) == 1 and "Settings" in setup_errors(pan)[0]
          and "Find models" in setup_errors(pan)[0], str(setup_errors(pan)))
    check("local: no word about Claude Code, uv or the bridge",
          not [t for t in setup_errors(pan) if "Claude" in t or "uv " in t or "bridge" in t])
    pan.send("make a box")
    spin(50)
    check("local without a model: nothing is sent, the message is not doubled",
          not [c for c in stub.calls if c[0] == "start"] and len(setup_errors(pan)) == 1
          and not kinds(pan, "user"))
    p.SetString("LocalUrl", "")
    p.SetString("LocalModel", "google/gemma-4-e4b")
    check("local without a server address: says so, the old message is gone",
          pan.check_setup() is False and len(setup_errors(pan)) == 1 and "server address" in setup_errors(pan)[0],
          str(setup_errors(pan)))
    p.SetString("LocalUrl", "http://127.0.0.1:1234/v1")
    check("local with address and model: ready, the setup messages are gone",
          pan.check_setup() is True and not setup_errors(pan))
    real_flatpak = paths.in_flatpak
    paths.in_flatpak = lambda: True
    check("local works in a Flatpak", pan.check_setup() is True and not setup_errors(pan))
    paths.in_flatpak = real_flatpak
    check("local: Claude Code, uv and the bridge were never looked at",
          len(bridge_calls) == n_bridge and len(claude_calls()) == n_claude and not asked and pan.claude is None)

    starts = []                              # ClaudeRun.start must not be reached from here on
    real_start = runner.ClaudeRun.start
    runner.ClaudeRun.start = lambda self, *a, **k: starts.append((a, k))
    projects.set_session(project, "claude-session-keep")
    stub.replay[:] = [("tool", "run_python", "box = doc.addObject('Part::Box', 'Box')"),
                      ("text", "A box 10 x 20 x 30 mm."),
                      ("finished", {"is_error": False, "result": "A box 10 x 20 x 30 mm."})]
    pan.input.setPlainText("make a box 10 x 20 x 30")
    pan.send()
    check("local: send() goes to LocalRun.start(project, text, engine_config()), with no image",
          [c for c in stub.calls if c[0] == "start"] == [("start", project, "make a box 10 x 20 x 30",
           {"kind": "openai", "url": "http://127.0.0.1:1234/v1", "model": "google/gemma-4-e4b", "key": ""})]
          and stub.images[-1] == [], str(stub.calls))
    check("local: ClaudeRun.start is not called, the bridge is not asked",
          not starts and len(bridge_calls) == n_bridge)
    check("local: while it works: 'The local model is working…', the button says Stop, the input is empty",
          pan.status.text() == "The local model is working…" and pan.send_btn.text() == "Stop"
          and pan.input.toPlainText() == "", pan.status.text())
    pan.send("second message while busy")
    check("local: a second message while busy is ignored", len([c for c in stub.calls if c[0] == "start"]) == 1)
    wait_for(lambda: pan.send_btn.text() == "Send", 3)
    check("local: the entries appear: you, tool, answer", pan.entries[-3:] == [
        ("user", "make a box 10 x 20 x 30"), ("tool", "`run_python` box = doc.addObject('Part::Box', 'Box')"),
        ("claude", "A box 10 x 20 x 30 mm.")], str(pan.entries[-3:]))
    check("local: when done the button says Send and the status is empty",
          pan.send_btn.text() == "Send" and pan.status.text() == "")
    check("local: the Claude Code session file is not touched", projects.get_session(project) == "claude-session-keep")
    shown = pan.view.toPlainText()
    check("local: the view shows the conversation", "make a box 10 x 20 x 30" in shown and "A box 10 x 20 x 30 mm." in shown)

    stub.replay[:] = [("finished", {"is_error": False, "result": "ok", "session_id": "from-a-broken-engine"})]
    pan.send("again")
    wait_for(lambda: pan.send_btn.text() == "Send", 3)
    check("local: even a session_id in the result is not stored",
          projects.get_session(project) == "claude-session-keep"
          and open(os.path.join(project, ".cadprompt", "session")).read().strip() == "claude-session-keep")

    stub.replay[:] = [("tool", "run_python", "x"), ("tool_error", "run_python")]
    pan.send("break it")
    spin(100)
    check("local: status names the tool: " + pan.status.text(),
          pan.status.text() == "The local model is working… (run_python)")
    check("local: a failed tool: 'the local model is looking at why'",
          kinds(pan, "error")[-1] == "`run_python` failed, the local model is looking at why", str(kinds(pan, "error")))
    n_err = len(kinds(pan, "error"))
    pan.send_btn.click()                     # Stop
    spin(50)
    check("local: Stop calls LocalRun.stop()", stub.calls[-1] == ("stop",), str(stub.calls[-1]))
    check("local: stopping is quiet: a note, no error, button back to Send",
          pan.entries[-1] == ("note", "Stopped.") and len(kinds(pan, "error")) == n_err
          and pan.send_btn.text() == "Send" and pan.status.text() == "", str(pan.entries[-1]))

    for res, want in (
        ({"is_error": True, "result": "The model server at http://127.0.0.1:1234/v1 does not answer. Is it running?"},
         "The model server at http://127.0.0.1:1234/v1 does not answer. Is it running?"),
        ({"is_error": True, "result": ""}, "The local model stopped without an answer."),
    ):
        stub.replay[:] = [("finished", res)]
        pan.send("hello")
        wait_for(lambda: pan.send_btn.text() == "Send", 3)
        check(f"local error is shown as it is: '{want[:40]}…'", pan.entries[-1] == ("error", want), str(pan.entries[-1]))
    check("local: no error text says 'Claude'", not [t for t in kinds(pan, "error") if "Claude" in t],
          str(kinds(pan, "error")))
    stub.replay[:] = [("finished", {"is_error": True, "result": "please login: authentication failed"})]
    pan.send("hello")
    wait_for(lambda: pan.send_btn.text() == "Send", 3)
    check("local: no 'run claude and sign in' hint on a local error",
          pan.entries[-1] == ("error", "please login: authentication failed"), str(pan.entries[-1]))

    # buttons: the same slash commands for every source
    stub.replay[:] = [("finished", {"is_error": False, "result": ""})]
    buttons = {b.text(): b for b in pan.findChildren(QtWidgets.QPushButton)}
    buttons["Check"].click()
    wait_for(lambda: pan.send_btn.text() == "Send", 3)
    check("local: Check sends /check", stub.calls[-1][:3] == ("start", project, "/check"), str(stub.calls[-1]))
    sizes = {a.text(): a for a in buttons["Drawing"].menu().actions()}
    sizes["A3"].trigger()
    wait_for(lambda: pan.send_btn.text() == "Send", 3)
    check("local: Drawing > A3 sends /drawing A3", list(sizes) == ["A2", "A3", "A4", "A5"]
          and stub.calls[-1][:3] == ("start", project, "/drawing A3"), str(stub.calls[-1]))

    # modes: the engine writes the mode file for /teach, the panel follows it
    stub.replay[:] = [("finished", {"is_error": False, "result": ""})]
    projects.set_mode(project, "teach")       # what the engine does for "/teach"
    pan.send("/teach")
    wait_for(lambda: pan.send_btn.text() == "Send", 3)
    check("local: after /teach the Teach button is on", pan.teach_btn.isChecked() and not pan.build_btn.isChecked())
    pan.build_btn.click()
    check("mode buttons still write the mode file", projects.get_mode(project) == "build")

    # references
    ref = os.path.join(home, "sketch.txt")
    open(ref, "w").write("flange, 6 holes\n")
    mime = QtCore.QMimeData()
    mime.setUrls([QtCore.QUrl.fromLocalFile(ref)])
    took = pan.take_mime(mime)
    refs = [f for f in os.listdir(os.path.join(project, "refs")) if f.endswith("sketch.txt")]
    check("local: a dropped file goes to refs/ and the note names the local model",
          took and len(refs) == 1 and pan.entries[-1][1].endswith("Tell the local model what to do with them."),
          str(pan.entries[-1]))
    check("local: tooltips name the local model", pan.build_btn.toolTip() == "The local model builds the whole part"
          and pan.refs_btn.toolTip().endswith("for the local model"))

    # images that were added go with the next message; other files stay references only
    def drop(*names, text=None):
        """Drop files with these names on the panel. Returns their names in refs/."""
        files = []
        for name in names:
            path = os.path.join(home, name)
            if text is None:
                picture = QtGui.QImage(12, 8, QtGui.QImage.Format_RGB32)
                picture.fill(QtCore.Qt.white)
                picture.save(path, "PNG")
            else:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(text)
            files.append(path)
        before = set(os.listdir(os.path.join(project, "refs")))
        mime = QtCore.QMimeData()
        mime.setUrls([QtCore.QUrl.fromLocalFile(f) for f in files])
        pan.take_mime(mime)
        return sorted(set(os.listdir(os.path.join(project, "refs"))) - before)

    def sent_with(text, replay=None):
        """Send a message; (the images the engine got or None when nothing was sent, new entries)."""
        stub.replay[:] = replay or [("finished", {"is_error": False, "result": ""})]
        n, e = len(stub.images), len(pan.entries)
        pan.input.setPlainText(text)
        pan.send()
        wait_for(lambda: pan.send_btn.text() == "Send", 3)
        return (stub.images[-1] if len(stub.images) > n else None), pan.entries[e:]

    (png,) = drop("sketch photo.png")
    check("local: a dropped image goes to refs/, and the note says it goes with the next message",
          pan.entries[-1] == ("note", f"In refs/: `{png}`. The image goes to the local model with your next message."),
          str(pan.entries[-1]))
    drop("datasheet.pdf", text="%PDF-1.4 made up")
    check("local: a PDF stays a reference only",
          pan.entries[-1][1].endswith("Tell the local model what to do with them."), str(pan.entries[-1]))
    got, new = sent_with("what is this part?")
    check("local: the image goes with the next message, the PDF and the text file do not",
          got == ["refs/" + png], str(got))
    check("local: the chat shows a small note under the message: 'with 1 image'",
          new[:2] == [("user", "what is this part?"), ("note", "with 1 image")], str(new))
    got, new = sent_with("and its size?")
    check("local: the image goes one time only", got == [] and ("note", "with 1 image") not in new, str((got, new)))
    six = drop(*["view %d.png" % i for i in range(6)])
    got, new = sent_with("six views")
    check("local: at most 4 images go with a message, the newest ones, and the panel says so",
          got == ["refs/" + n for n in six[2:]] and ("note", "with 4 images") in new
          and any(k == "note" and "Only the newest 4 images" in t for k, t in new), str((got, new)))
    (bad,) = drop("not a picture.png", text="this is text")
    got, new = sent_with("look at this")
    check("local: a file that is no image: a plain error, nothing is sent, the text stays in the input",
          got is None and len(new) == 1 and new[0][0] == "error" and "is not an image" in new[0][1]
          and "Traceback" not in new[0][1] and pan.input.toPlainText() == "look at this", str((got, new)))
    pan.send()
    wait_for(lambda: pan.send_btn.text() == "Send", 3)
    check("local: Send again sends the message without that file",
          stub.calls[-1][:3] == ("start", project, "look at this") and stub.images[-1] == [], str(stub.calls[-1]))
    drop("flange.png")
    hint = "This model cannot read images. Send your message again and it goes without the images."
    got, new = sent_with("which flange is this?", [("finished", {"is_error": True, "result": hint, "no_vision": True})])
    check("local, a model without vision: the hint is shown and the message is back in the input",
          len(got) == 1 and new[-1] == ("error", hint) and pan.input.toPlainText() == "which flange is this?",
          str((got, new, pan.input.toPlainText())))
    n = len(stub.images)
    spin(100)
    check("local, a model without vision: nothing is sent again by itself", len(stub.images) == n)
    stub.replay[:] = [("finished", {"is_error": False, "result": ""})]
    pan.send()
    wait_for(lambda: pan.send_btn.text() == "Send", 3)
    check("local, a model without vision: Send again sends the message without the images",
          stub.calls[-1][:3] == ("start", project, "which flange is this?") and stub.images[-1] == []
          and pan.input.toPlainText() == "", str(stub.calls[-1]))

    # history and New chat
    stub.past[project] = [("user", "earlier question"), ("tool", "`run_python` x"), ("claude", "earlier answer")]
    del stub.calls[:]
    pan._select("test-part", open_model=False)
    check("local: the earlier conversation comes from local_engine.history(project)",
          ("history", project) in stub.calls and pan.entries == [
              ("note", "Project **test-part**."), ("user", "earlier question"), ("tool", "`run_python` x"),
              ("claude", "earlier answer"), ("note", "Continuing this conversation.")], str(pan.entries))
    newchat = [b for b in pan.findChildren(QtWidgets.QToolButton) if b.text() == "New chat"][0]
    newchat.click()
    check("local: New chat calls local_engine.clear(project)", stub.calls[-1] == ("clear", project), str(stub.calls))
    check("local: New chat empties the view and leaves the Claude Code session alone",
          pan.entries == [("note", "New conversation.")] and projects.get_session(project) == "claude-session-keep")
    stub.replay[:] = [("text", "half an answer")]          # a run that does not end
    pan.send("long job")
    spin(50)
    newchat.click()
    check("local: New chat while the model works stops it first, quietly",
          stub.calls[-2:] == [("stop",), ("clear", project)] and pan.entries == [("note", "New conversation.")]
          and pan.send_btn.text() == "Send", str(stub.calls[-3:]) + str(pan.entries))
    stub.replay[:] = [("text", "half an answer")]
    pan.send("long job")
    spin(50)
    pan._select("other-part", open_model=False)
    check("local: changing the project stops the run, quietly",
          ("stop",) in stub.calls[-3:] and pan.send_btn.text() == "Send" and pan.project == other
          and not kinds(pan, "error") and ("note", "Stopped.") not in pan.entries,
          str((stub.calls[-4:], pan.send_btn.text(), pan.project, pan.entries)))
    pan._select("test-part", open_model=False)
    check("local: ClaudeRun.start was never called in all of this", not starts)
    paths.find_tool = real_find_tool
    bridge_ok[0] = True

    # =====================================================================
    say("\n-- panel: settings change the source --")
    def settings(change):
        """Open the real dialog as the panel does, let `change` click in it, press OK."""
        def fake_exec(dlg):
            change(dlg)
            dlg._save()
            return dlg.result()
        SettingsDialog.exec = fake_exec
        try:
            pan._settings()
        finally:
            del SettingsDialog.exec
        spin(100)

    p.SetString("LocalModel", "")
    pan.check_setup()
    check("(setup) local without a model shows its message", len(setup_errors(pan)) == 1)
    settings(lambda d: d.local_model.setEditText("google/gemma-4-e4b"))
    check("after Settings > OK the check runs again: the stale message is gone",
          not setup_errors(pan) and pan.source.full == "Local model · google/gemma-4-e4b", pan.source.full)
    os.environ["PANEL_FAKE_LOGGED_IN"] = "0"
    n_claude = len(claude_calls())
    del stub.calls[:]
    settings(lambda d: d.account.click())
    wait_for(lambda: len(claude_calls()) > n_claude and setup_errors(pan), 5)
    auth = claude_calls()[n_claude:]
    check("switch to the account: the label follows, the Claude Code checks run",
          pan.source.full == "Claude account · sonnet" and pan.claude == fake_claude
          and [c["argv"] for c in auth] == [["auth", "status"]], pan.source.full + str([c["argv"] for c in auth]))
    check("`claude auth status` runs without the shell's key, token and endpoint",
          bool(auth) and not [k for k in auth[0]["env"] if k in (
              "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL")], str(auth and auth[0]["env"]))
    check("account, not signed in: the old message", len(setup_errors(pan)) == 1
          and setup_errors(pan)[0].startswith("Claude Code is not signed in."), str(setup_errors(pan)))
    check("switch to the account: the view shows the Claude Code conversation, not the local one",
          ("history", project) not in stub.calls and pan.entries[0] == ("note", "Project **test-part**."))
    stub.past[project] = [("user", "local question"), ("claude", "local answer")]
    settings(lambda d: d.local.click())
    check("switch to local: 'not signed in' is gone, the local conversation is loaded",
          not setup_errors(pan) and ("user", "local question") in pan.entries
          and pan.source.full.startswith("Local model"), str(pan.entries))
    n_claude = len(claude_calls())
    settings(lambda d: (d.apikey.click(), d.api_key.drop.click()))
    spin(300)
    check("switch to API key without a key: says where to put it",
          setup_errors(pan) == ["No Anthropic API key is saved. Open Settings and paste your key."]
          and backends.api_key() == "", str(setup_errors(pan)))
    check("the API key goes on with the conversation of the built-in engine, the same as the local model",
          ("user", "local question") in pan.entries, str(pan.entries))
    del stub.calls[:]
    pan.send("hello")
    spin(50)
    check("API key without a key: nothing is sent, the message is shown once",
          not starts and not [c for c in stub.calls if c[0] == "start"]
          and len(setup_errors(pan)) == 1 and "hello" not in kinds(pan, "user"))
    paths.find_tool = lambda name: None          # no Claude Code, no uv on this computer
    settings(lambda d: d.api_key.edit.setText("sk-ant-made-up-3"))
    spin(300)
    check("API key saved: ready without Claude Code and uv; no program is started, no sign-in is checked",
          not setup_errors(pan) and len(claude_calls()) == n_claude
          and pan.source.full == "Anthropic API key · claude-haiku-5-5", str(setup_errors(pan)) + pan.source.full)
    check("API key: tooltips name Claude", pan.build_btn.toolTip() == "Claude builds the whole part")
    paths.in_flatpak = lambda: True
    check("API key in a Flatpak: ready too, no program has to be started", pan.check_setup() is True
          and not setup_errors(pan), str(setup_errors(pan)))
    paths.in_flatpak = real_flatpak

    # a message with the API key goes to the built-in engine; Claude Code is not started
    n_bridge, n_err = len(bridge_calls), len(kinds(pan, "error"))
    del stub.calls[:]
    stub.replay[:] = [("tool", "run_python", "shaft()"), ("tool_error", "run_python"),
                      ("text", "A shaft."), ("finished", {"is_error": False, "result": "A shaft."})]
    pan.input.setPlainText("make a shaft")
    pan.send()
    working = pan.status.text()
    wait_for(lambda: pan.send_btn.text() == "Send", 3)
    check("apikey: send() goes to LocalRun.start with the Anthropic config: fixed address, the model, the API key",
          [c for c in stub.calls if c[0] == "start"] == [("start", project, "make a shaft", {
              "kind": "anthropic", "url": "https://api.anthropic.com", "model": "claude-haiku-5-5",
              "key": "sk-ant-made-up-3"})], str(stub.calls))
    check("apikey: Claude Code is not started and the bridge is not asked",
          not starts and len(claude_calls()) == n_claude and len(bridge_calls) == n_bridge,
          f"{starts} {len(claude_calls()) - n_claude} {len(bridge_calls) - n_bridge}")
    check("apikey: the words name Claude, never 'the local model'",
          working == "Claude is working…" and kinds(pan, "error")[n_err:] == ["`run_python` failed, Claude is looking at why"]
          and ("claude", "A shaft.") in pan.entries, f"{working!r} {kinds(pan, 'error')[n_err:]}")
    stub.replay[:] = [("finished", {"is_error": True, "result": ""})]
    pan.send("hello")
    wait_for(lambda: pan.send_btn.text() == "Send", 3)
    check("apikey: an error without a text names Claude", pan.entries[-1] == ("error", "Claude stopped without an answer."),
          str(pan.entries[-1]))
    (png,) = drop("bracket.png")
    check("apikey: an added image goes with the next message to Claude",
          pan.entries[-1][1].endswith("The image goes to Claude with your next message."), str(pan.entries[-1]))
    got, new = sent_with("what is this?")
    check("apikey: the image goes to the engine with the message", got == ["refs/" + png]
          and ("note", "with 1 image") in new, str((got, new)))
    [b for b in pan.findChildren(QtWidgets.QToolButton) if b.text() == "New chat"][0].click()
    check("apikey: New chat clears the engine's conversation, not the Claude Code session",
          stub.calls[-1] == ("clear", project) and projects.get_session(project) == "claude-session-keep",
          str(stub.calls[-1]))
    paths.find_tool = real_find_tool
    os.environ["PANEL_FAKE_LOGGED_IN"] = "1"

    # =====================================================================
    say("\n-- panel: Claude Code sources (ClaudeRun.start replaced by a recorder) --")
    del stub.calls[:]
    shell = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL")

    def send_recorded(backend, text):
        p.SetString("Backend", backend)
        del starts[:]
        pan.check_setup()
        pan.send(text)
        if not starts:
            return None, None
        (claude, proj, prompt, session, env), _kw = starts[-1]
        pe = runner.claude_environment(claude, env)      # exactly what ClaudeRun.start gives the process
        pan.run.finished.emit({"is_error": False, "result": "ok"})
        return (claude, proj, prompt, session, env), pe

    args, pe = send_recorded("account", "make a flange")
    check("account: send() goes to ClaudeRun.start(claude, project, text, session, env)",
          args is not None and args[:4] == (fake_claude, project, "make a flange", "claude-session-keep"), str(args))
    check("account: LocalRun is not used", not [c for c in stub.calls if c[0] == "start"])
    check("account: the address and the token of the bridge are handed over, and nothing else",
          args[4] == {"CADPROMPT_MCP_URL": "http://127.0.0.1:1/mcp", "CADPROMPT_MCP_TOKEN": "test-token"}, str(args[4]))
    check("account: the bridge is asked for this project and this source, right before Claude Code starts",
          bridge_calls[-1] == (project, "account"), str(bridge_calls[-1:]))
    check("account: ANTHROPIC_API_KEY is set in os.environ for this test",
          os.environ.get("ANTHROPIC_API_KEY") == "shell-key-must-not-leak"
          and QtCore.QProcessEnvironment.systemEnvironment().value("ANTHROPIC_API_KEY") == "shell-key-must-not-leak")
    check("account: the process environment has ANTHROPIC_API_KEY, _AUTH_TOKEN and _BASE_URL removed",
          not [k for k in shell if pe.contains(k)] and pe.value("CADPROMPT_MCP_TOKEN") == "test-token",
          str([k for k in shell if pe.contains(k)]))
    check("account: the user's other variables are still there",
          pe.value("HOME") == (os.environ.get("HOME") or "") and pe.value("PANEL_BUNDLE_OTHER") == "kept"
          and pe.value("PATH").split(os.pathsep)[0] == bindir, pe.value("PATH")[:300])
    inside = [k for k in pe.keys() if bundle in pe.value(k)]
    check("account: nothing that points into the FreeCAD bundle is handed on (PYTHONPATH, a certificate file, PATH)",
          not inside and not pe.contains("PYTHONPATH") and not pe.contains("PANEL_BUNDLE_CERT"), str(inside))
    check("account: FreeCAD's own PYTHONHOME is not handed on",
          not pe.contains("PYTHONHOME") or not real_roots, pe.value("PYTHONHOME"))

    stub.replay[:] = [("finished", {"is_error": False, "result": ""})]
    args, pe = send_recorded("apikey", "make a shaft")
    check("apikey: does not go to ClaudeRun.start", args is None and stub.calls[-1][:3] == ("start", project, "make a shaft"),
          str((args, stub.calls[-1])))
    wait_for(lambda: pan.send_btn.text() == "Send", 3)

    args, pe = send_recorded("custom", "make a pin")
    check("custom: goes to ClaudeRun.start with the endpoint from Settings and no API key",
          args is not None and pe.value("ANTHROPIC_BASE_URL") == "http://localhost:11434"
          and pe.value("ANTHROPIC_AUTH_TOKEN") == "endpoint-token" and pe.value("ANTHROPIC_MODEL") == "endpoint-model"
          and not pe.contains("ANTHROPIC_API_KEY"))
    check("custom: the old backend_env() still answers", runner.backend_env().get("ANTHROPIC_MODEL") == "endpoint-model")
    check("custom: the bridge is asked with the source 'custom'", bridge_calls[-1] == (project, "custom"),
          str(bridge_calls[-1:]))

    # the Claude Code result handling is as before
    p.SetString("Backend", "account")
    pan.check_setup()
    del starts[:]
    pan.send("make a flange")
    check("account: while it works: 'Claude is working…' and Stop",
          pan.status.text() == "Claude is working…" and pan.send_btn.text() == "Stop")
    pan.run.tool.emit("run_python", "doc.recompute()")
    pan.run.tool_error.emit("run_python")
    check("account: tool status and tool error name Claude",
          pan.status.text() == "Claude is working… (run_python)"
          and pan.entries[-1] == ("error", "`run_python` failed, Claude is looking at why"), str(pan.entries[-1]))
    pan.run.finished.emit({"is_error": False, "result": "ok", "session_id": "claude-session-new"})
    check("account: the session id of the result is stored, as before",
          projects.get_session(project) == "claude-session-new" and pan.send_btn.text() == "Send")
    pan.send("more")
    pan.run.finished.emit({"is_error": True, "result": "Invalid API key · Please run /login"})
    check("account: a login error still gets the sign-in hint",
          pan.entries[-1][0] == "error" and "run `claude`, and sign in" in pan.entries[-1][1], str(pan.entries[-1]))
    pan.send("more")
    pan.run.finished.emit({"is_error": False, "result": "", "permission_denials": [{"tool_name": "WebSearch"}]})
    check("account: a blocked web tool still points to Settings",
          pan.entries[-1] == ("error", "`WebSearch` was blocked: web access is off. "
                                       "Turn it on in Settings, then ask again."), str(pan.entries[-1]))
    check("account: tooltips and notes name Claude", pan.build_btn.toolTip() == "Claude builds the whole part"
          and pan.refs_btn.toolTip() == "Copy photos, PDFs or drawings into refs/ for Claude")
    mime = QtCore.QMimeData()
    mime.setText("https://example.com/datasheet.pdf")
    check("account: a pasted link becomes a reference, 'Tell Claude…'", pan.take_mime(mime)
          and pan.entries[-1][1].endswith("Tell Claude what to do with them.")
          and "example.com/datasheet.pdf" in open(os.path.join(project, "refs", "links.md")).read())
    [b for b in pan.findChildren(QtWidgets.QToolButton) if b.text() == "New chat"][0].click()
    check("account: New chat forgets the Claude Code session and does not clear the local conversation",
          projects.get_session(project) is None and ("clear", project) not in stub.calls)
    paths.in_flatpak = lambda: True
    check("account in a Flatpak: the old message, plus the way out",
          pan.check_setup() is False and len(setup_errors(pan)) == 1 and "Flatpak" in setup_errors(pan)[0]
          and "a local model or an API key in Settings" in setup_errors(pan)[0], str(setup_errors(pan)))
    paths.in_flatpak = real_flatpak
    paths.find_tool = lambda name: None
    check("account without Claude Code: the install hint is still shown",
          pan.check_setup() is False and setup_errors(pan)[0].startswith("Claude Code is not installed."),
          str(setup_errors(pan)))
    paths.find_tool = real_find_tool
    pan.check_setup()
    spin(300)

    # =====================================================================
    say("\n-- panel: a project that an older CADprompt made --")
    with open(os.path.join(HERE, "old_template.json"), encoding="utf-8") as f:
        old_files = json.load(f)["before"]
    aged = projects.create("Aged part")
    for rel in (".mcp.json", "CLAUDE.md"):
        with open(os.path.join(aged, rel), "w", encoding="utf-8", newline="") as f:
            f.write(old_files[rel])
    p.SetString("Backend", "account")
    pan._select("aged-part", open_model=False)
    with open(os.path.join(aged, ".mcp.json"), encoding="utf-8") as f:
        now = f.read()
    check("a project with the old files: picking it brings .mcp.json and CLAUDE.md up to date, the old ones are kept",
          "CADPROMPT_MCP_URL" in now and "uvx" not in now and os.path.isfile(os.path.join(aged, ".mcp.json.old"))
          and os.path.isfile(os.path.join(aged, "CLAUDE.md.old")) and not kinds(pan, "setup-note"),
          now[:200] + str(kinds(pan, "setup-note")))
    edited = projects.create("Edited part")
    mine = old_files["CLAUDE.md"] + "\n## My shop\n\nOnly M6 and M8 screws.\n"
    with open(os.path.join(edited, "CLAUDE.md"), "w", encoding="utf-8", newline="") as f:
        f.write(mine)
    pan._select("edited-part", open_model=False)
    notes = kinds(pan, "setup-note")
    with open(os.path.join(edited, "CLAUDE.md"), encoding="utf-8", newline="") as f:
        check("a CLAUDE.md that the user changed is left alone", f.read() == mine
              and not os.path.exists(os.path.join(edited, "CLAUDE.md.old")))
    check("... and the panel says one time, in plain words, that it names old tools",
          len(notes) == 1 and "`CLAUDE.md`" in notes[0] and "old tools" in notes[0] and "`run_python`" in notes[0]
          and pan.entries[0] == ("note", "Project **edited-part**."), str(notes))
    pan.check_setup()
    spin(300)
    check("the note is still there after the next setup check, one time",
          kinds(pan, "setup-note") == notes and "names old tools" in pan.view.toPlainText().replace("\n", " "),
          str(kinds(pan, "setup-note")))
    p.SetString("Backend", "local")
    pan._select("edited-part", open_model=False)
    pan.check_setup()
    check("with a local model the note is not shown: the built-in engine does not read CLAUDE.md",
          not kinds(pan, "setup-note"), str(kinds(pan, "setup-note")))
    p.SetString("Backend", "account")
    pan._select("test-part", open_model=False)
    check("back in a project of today: no note", not kinds(pan, "setup-note") and pan.project == project)
    pan.check_setup()
    spin(300)

    # =====================================================================
    say("\n-- panel: the real ClaudeRun with the pretend claude program and the real bridge --")
    runner.ClaudeRun.start = real_start
    bridge.ensure = real_ensure
    fresh = new_panel(400)                       # a panel with the Claude account, set up and signed in
    check("the bridge does not listen before the first message goes to Claude Code: not when the panel "
          "opens, not after the setup check", not bridge.running() and fresh.claude == fake_claude
          and not setup_errors(fresh), str(setup_errors(fresh)))

    def run_fake(backend, text):
        p.SetString("Backend", backend)
        pan.check_setup()
        spin(300)                                # lets `auth status` of the account finish
        n = len(claude_calls())
        pan.send(text)
        wait_for(lambda: pan.send_btn.text() == "Send" and len(claude_calls()) > n, 15)
        runs = [c for c in claude_calls()[n:] if c["argv"][:1] == ["-p"]]
        return runs[-1] if runs else {"argv": [], "env": {}, "stdin": "", "cwd": "", "path0": "", "path": []}

    p.SetBool("AllowWeb", True)
    rec = run_fake("account", "make a bracket")
    check("account: the program gets the prompt on stdin, in the project folder",
          rec["stdin"] == "make a bracket" and os.path.realpath(rec["cwd"]) == os.path.realpath(project), str(rec))
    check("account: the program is found in a folder with a space and a letter that is not ASCII",
          rec["path0"] == bindir and " " in bindir, f"{rec['path0']!r} {bindir!r}")
    check("account: the started program got nothing that points into the FreeCAD bundle",
          not [k for k, v in rec["env"].items() if bundle in v] and "PYTHONPATH" not in rec["env"]
          and "PYTHONHOME" not in rec["env"] and not [d for d in rec["path"] if d.startswith(bundle)]
          and rec["env"].get("PANEL_BUNDLE_OTHER") == "kept",
          str({k: v for k, v in rec["env"].items() if k.startswith(("PYTHON", "PANEL_"))}) + str(rec["path"][:3]))
    freecad_tools = ["mcp__freecad__run_python", "mcp__freecad__get_document_state", "mcp__freecad__make_drawing",
                     "mcp__freecad__get_view"]
    check("account: arguments: stream-json, the model, and the tools that need no question of Claude Code: "
          "the four FreeCAD tools by name, and the web tools because they are on in Settings",
          rec["argv"] == ["-p", "--output-format", "stream-json", "--verbose", "--model", "sonnet",
                          "--allowedTools"] + freecad_tools + ["WebSearch", "WebFetch"], str(rec["argv"]))
    check("account: the started program has no ANTHROPIC_API_KEY, _AUTH_TOKEN, _BASE_URL",
          not [k for k in shell if k in rec["env"]], str(rec["env"]))
    url1, token1 = rec["env"].get("CADPROMPT_MCP_URL", ""), rec["env"].get("CADPROMPT_MCP_TOKEN", "")
    check("account: the started program has the address and the token of the bridge in its environment",
          re.fullmatch(r"http://127\.0\.0\.1:\d+/mcp", url1) is not None and len(token1) >= 43, f"{url1} {len(token1)}")
    check("account: nothing of the old bridge is handed over, and no uvx program is on this computer",
          not [k for k in rec["env"] if k.startswith("FREECAD_MCP") or k == "CADPROMPT_FREECADCMD"]
          and shutil.which("uvx", path=bindir) is None and paths.params().GetString("uvxPath", "") == "", str(rec["env"]))
    check("account: the bridge listens since that message, and stays for the next one", bridge.running())
    check("account: answer, tool line and session id arrive in the panel",
          ("claude", "fake answer to: make a bracket") in pan.entries
          and ("tool", "`run_python` doc.recompute()") in pan.entries
          and projects.get_session(project) == "fake-session-1", str(pan.entries[-3:]))
    n = len(claude_calls())
    p.SetString("Backend", "apikey")
    pan.check_setup()
    got, new = sent_with("make a lever")
    spin(300)
    check("apikey: no program is started, the saved key is in no program's environment",
          got == [] and len(claude_calls()) == n and projects.get_session(project) == "fake-session-1"
          and "sk-ant-made-up-3" not in json.dumps(claude_calls()), str(claude_calls()[n:]))
    check("apikey: the bridge is closed when the source is not Claude Code any more", not bridge.running())
    rec = run_fake("custom", "make a knob")
    check("custom: the started program has the endpoint and no API key, and no Claude model argument",
          rec["env"].get("ANTHROPIC_BASE_URL") == "http://localhost:11434" and "ANTHROPIC_API_KEY" not in rec["env"]
          and "--model" not in rec["argv"], str(rec))
    check("custom: Claude Code on another endpoint gets the bridge too, with a new token",
          bridge.running() and len(rec["env"].get("CADPROMPT_MCP_TOKEN", "")) >= 43
          and rec["env"].get("CADPROMPT_MCP_TOKEN") != token1, str(rec["env"]))
    p.SetBool("AllowWeb", False)

    # the pretend claude does what Claude Code does: it reads .mcp.json and calls the FreeCAD tools
    for d in list(App.listDocuments()):
        App.closeDocument(d)
    rec = run_fake("account", "mcp: box = doc.addObject('Part::Box', 'FromClaude')\nbox.Length = 20")
    mcp = rec.get("mcp") or {}
    made = App.ActiveDocument.getObject("FromClaude") if App.ActiveDocument is not None else None
    check("during the run the started program reaches the bridge with the project's .mcp.json and its environment",
          mcp.get("type") == "http" and mcp.get("url") == rec["env"].get("CADPROMPT_MCP_URL")
          and (mcp.get("initialize") or [0])[0] == 200 and mcp.get("initialized") == [202, None]
          and "error" not in mcp, str(mcp)[:600])
    check("... it gets the four tools", mcp.get("tools") == ["run_python", "get_document_state", "make_drawing", "get_view"],
          str(mcp.get("tools")))
    call_status, call_body = mcp.get("call") or (0, {})
    check("... and its run_python call makes the object in this FreeCAD while the panel waits",
          call_status == 200 and made is not None and abs(made.Length.Value - 20) < 1e-9
          and "+ new FromClaude" in call_body["result"]["content"][0]["text"]
          and call_body["result"]["isError"] is False, str(mcp.get("call"))[:400])
    check("... a request without the token is refused", mcp.get("no_token") == 401, str(mcp.get("no_token")))
    check("with the web off in Settings only the four FreeCAD tools are allowed: no web tool, no Bash, no wildcard",
          rec["argv"][-5:] == ["--allowedTools"] + freecad_tools, str(rec["argv"]))
    check("the panel shows the tool call and the answer as before",
          ("tool", "`run_python` doc.recompute()") in pan.entries and pan.send_btn.text() == "Send")

    # a session id that Claude Code does not have any more (transcript deleted, other computer)
    projects.set_session(project, "gone-session")
    p.SetString("Backend", "account")
    pan.check_setup()
    spin(300)
    n = len(claude_calls())
    pan.send("make a washer")
    wait_for(lambda: pan.send_btn.text() == "Send", 15)
    runs = [c["argv"] for c in claude_calls()[n:] if c["argv"][:1] == ["-p"]]
    check("account, lost conversation: asked again without --resume",
          len(runs) == 2 and runs[0][4:6] == ["--resume", "gone-session"] and "--resume" not in runs[1], str(runs))
    check("account, lost conversation: the note and then the answer of the new run are shown",
          pan.entries[-4:-1] == [("user", "make a washer"),
                                 ("note", "The earlier conversation could not be found; starting a new one."),
                                 ("claude", "fake answer to: make a washer")], str(pan.entries[-4:]))
    check("account, lost conversation: the panel is idle again and the new session is stored",
          pan.send_btn.text() == "Send" and pan.status.text() == "" and not pan.run.running()
          and projects.get_session(project) == "fake-session-1",
          str((pan.send_btn.text(), pan.status.text(), projects.get_session(project))))

    # a prompt with letters that are not ASCII arrives as it was typed
    rec = run_fake("account", "make a Ø80 flange, 4 × M8, größer")
    check("account: a prompt with Ø, × and ö arrives as typed, and so does the answer",
          rec["stdin"] == "make a Ø80 flange, 4 × M8, größer"
          and ("claude", "fake answer to: make a Ø80 flange, 4 × M8, größer") in pan.entries,
          repr(rec["stdin"]) + str(pan.entries[-2:]))

    # Stop ends the program, and what the program started (Windows: the program behind claude.cmd)
    def alive(pid):
        if os.name == "nt":
            import ctypes
            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)     # PROCESS_QUERY_LIMITED_INFORMATION
            if not handle:
                return False
            code = ctypes.c_ulong()
            ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            ctypes.windll.kernel32.CloseHandle(handle)
            return code.value == 259                                             # STILL_ACTIVE
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True

    n = len(claude_calls())
    pan.send("slow: make a housing")
    started = wait_for(lambda: [c for c in claude_calls()[n:] if c["stdin"].startswith("slow")], 15)
    pid = ([c["pid"] for c in claude_calls()[n:] if c["stdin"].startswith("slow")] or [0])[0]
    check("account: (setup) a run that takes long is under way, the button says Stop",
          bool(started) and pid and alive(pid) and pan.send_btn.text() == "Stop", f"{pid} {pan.send_btn.text()}")
    pan.send_btn.click()                     # Stop
    stopped = wait_for(lambda: pan.send_btn.text() == "Send" and not alive(pid), 15)
    check("account: Stop ends the program that does the work, and the panel says 'Stopped.'",
          stopped and pan.entries[-1] == ("note", "Stopped."), f"alive={alive(pid) if pid else None} {pan.entries[-2:]}")

    # the two engines side by side
    p.SetString("Backend", "local")
    pan.check_setup()
    n = len(claude_calls())
    del stub.calls[:]
    stub.replay[:] = [("finished", {"is_error": False, "result": ""})]
    pan.send("back to local")
    wait_for(lambda: pan.send_btn.text() == "Send", 3)
    spin(200)
    check("back on local: the stub engine gets the message, claude is not started",
          stub.calls[-1][:3] == ("start", project, "back to local") and len(claude_calls()) == n
          and projects.get_session(project) == "fake-session-1")

    # the life of the bridge: only while Claude Code can need it
    say("\n-- panel: when the bridge listens --")
    port_of = lambda url: int(re.search(r":(\d+)/", url).group(1))

    def listens(port):
        import socket
        try:
            socket.create_connection(("127.0.0.1", port), timeout=2).close()
            return True
        except OSError:
            return False

    rec = run_fake("account", "make a pin")
    url2 = rec["env"].get("CADPROMPT_MCP_URL", "")
    check("(setup) a message to Claude Code: the bridge listens", bridge.running() and listens(port_of(url2)), url2)
    settings(lambda d: d.local.click())
    check("the source changes to the local model in Settings: the bridge stops and its port is closed",
          not bridge.running() and not listens(port_of(url2)))
    settings(lambda d: d.account.click())
    spin(300)
    check("back on the Claude account: still closed until the next message", not bridge.running())
    rec = run_fake("account", "make a pin again")
    url3, token3 = rec["env"].get("CADPROMPT_MCP_URL", ""), rec["env"].get("CADPROMPT_MCP_TOKEN", "")
    check("the next message starts it again, with a new token",
          bridge.running() and listens(port_of(url3)) and len(token3) >= 43 and token3 not in (token1, ""), url3)
    pan.show()
    spin(100)
    check("(setup) the panel is on screen, the bridge still listens", pan.isVisible() and bridge.running())
    pan.hide()
    spin(100)
    check("the panel is closed: the bridge stops", not bridge.running() and not listens(port_of(url3)))
    rec = run_fake("account", "mcp: closed = 1")
    check("a message from a closed panel still works, and the bridge stops again when Claude Code has ended",
          ((rec.get("mcp") or {}).get("call") or [0])[0] == 200 and not bridge.running(), str(rec.get("mcp"))[:300])
    pan.show()
    spin(100)
    n = len(claude_calls())
    pan.send("slow: make a housing")
    wait_for(lambda: [c for c in claude_calls()[n:] if c["stdin"].startswith("slow")], 15)
    check("(setup) Claude Code works, the bridge listens", pan.send_btn.text() == "Stop" and bridge.running())
    pan.hide()
    spin(100)
    check("the panel is closed while Claude Code works: the bridge stays for that run", bridge.running())
    pan.send_btn.click()                     # Stop
    wait_for(lambda: pan.send_btn.text() == "Send", 15)
    spin(200)
    check("... and stops when the run has ended", not bridge.running())
    pan.show()
    spin(100)
    bridge.ensure(project, "account")
    for one in panels:
        one.deleteLater()
    del panels[:]
    QtWidgets.QApplication.sendPostedEvents(None, int(QtCore.QEvent.DeferredDelete))
    spin(200)
    check("the panel is deleted: the bridge stops", not bridge.running())

    check("no exception in any Qt slot", not SLOT_ERRORS, "\n".join(SLOT_ERRORS))
    for pan in panels:
        pan.deleteLater()


def _hook(kind, value, tb):
    SLOT_ERRORS.append("".join(traceback.format_exception(kind, value, tb)))


if not os.environ.get("FREECAD_USER_HOME"):
    say("Set FREECAD_USER_HOME to an empty folder first: this test must not touch your FreeCAD settings.")
    os._exit(2)
sys.excepthook = _hook
faulthandler.dump_traceback_later(270, exit=True, file=OUT)     # a hang ends with a trace, not silence
try:
    main()
except Exception:
    say(traceback.format_exc())
    FAILED.append("the test script itself")
say(f"\n{COUNT[0] - len([f for f in FAILED if f != 'the test script itself'])} of {COUNT[0]} checks passed"
    + (", FAILED: " + "; ".join(FAILED) if FAILED else ", all good"))
os._exit(1 if FAILED else 0)
