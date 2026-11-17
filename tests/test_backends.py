# RUN: freecadcmd
"""Tests for cadprompt/backends.py and the Claude Code environment in runner.py.

Run headless, with a throw-away FreeCAD profile so no real setting is touched:

    FREECAD_USER_HOME=$(mktemp -d) timeout 300 freecadcmd tests/test_backends.py

No Claude Code and no real key is used: every key below is made up.
"""

import http.server
import json
import os
import stat
import sys
import threading
import time
import traceback

if not os.environ.get("FREECAD_USER_HOME"):
    sys.stdout.write("Set FREECAD_USER_HOME to a fresh folder first: this test overwrites the saved keys.\n")
    sys.stdout.flush()
    os._exit(2)

sys.dont_write_bytecode = True       # no __pycache__ in the repository
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from utf8_output import utf8_output            # noqa: E402

utf8_output()

FAILED = []
COUNT = [0]
POSIX = os.name == "posix"


def check(name, ok, detail=""):
    COUNT[0] += 1
    if not ok:
        FAILED.append(name)
    print(("ok    " if ok else "FAIL  ") + name + (f"   [{detail}]" if detail and not ok else ""), flush=True)


class _Fake(http.server.BaseHTTPRequestHandler):
    """A pretend model server: /v1 answers like LM Studio, the other paths misbehave."""
    seen = []
    claude = []       # (x-api-key, anthropic-version) of the requests to /anthropic

    def log_message(self, *a):
        pass

    def do_GET(self):
        _Fake.seen.append((self.path, self.headers.get("Authorization")))
        if self.path.startswith("/anthropic/v1/models"):
            _Fake.claude.append((self.headers.get("x-api-key"), self.headers.get("anthropic-version")))
            if self.headers.get("x-api-key") != "sk-ant-made-up":
                self.send_response(401)
                self.end_headers()
                return
            body = {"data": [{"type": "model", "id": "claude-fake-1"}, {"type": "model", "id": "claude-fake-2"}],
                    "has_more": False}
        elif self.path == "/v1/models":
            body = {"object": "list", "data": [{"id": "google/gemma-4-e4b", "object": "model"},
                                               {"id": "other-model"}, {"id": "google/gemma-4-e4b"}]}
        elif self.path == "/ollama/models":
            body = {"models": [{"name": "llama3:8b"}]}
        elif self.path == "/locked/models":
            if self.headers.get("Authorization") != "Bearer right-key":
                self.send_response(401)
                self.end_headers()
                return
            body = {"data": [{"id": "paid-model"}]}
        elif self.path == "/slow/models":
            time.sleep(4)
            body = {"data": []}
        elif self.path.startswith("/redirect"):       # /redirect/<code>/<port>/models
            _, _, code, port, _ = self.path.split("/")
            self.send_response(int(code))
            self.send_header("Location", "http://localhost:%s/v1/models" % port)
            self.end_headers()
            return
        elif self.path == "/text/models":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"<html>not a model list</html>")
            return
        else:
            body = {"error": "Unexpected endpoint or method."}
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class _Proxy(http.server.BaseHTTPRequestHandler):
    """A pretend proxy of the school network. It writes down what reaches it and passes nothing on."""
    seen = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        _Proxy.seen.append((self.path, self.headers.get("Authorization")))
        self.send_response(502)
        self.end_headers()

    do_CONNECT = do_GET


def inside(path, folder):
    """True when path is the folder or in it. Not a comparison of two texts: Windows mixes \\ and /
    and upper and lower case, macOS has two names for its temporary folder."""
    path, folder = (os.path.normcase(os.path.realpath(x)) for x in (path, folder))
    try:
        return os.path.commonpath([path, folder]) == folder
    except ValueError:           # two drives
        return False


def raises(kinds, fn):
    try:
        fn()
    except kinds as e:
        return type(e).__name__
    except Exception as e:      # the wrong kind of error
        return False
    return False


def main():
    import FreeCAD as App
    from cadprompt import backends, local_agent, paths

    home = os.environ["FREECAD_USER_HOME"]
    data = App.getUserAppDataDir()
    print("FreeCAD user data:", data)
    if not inside(data, home):
        # a FreeCAD that does not follow the variable: stop before any real key file is touched
        check("FreeCAD's user data folder is inside FREECAD_USER_HOME", False, data)
        return
    p = paths.params()

    # ---- which source ----
    check("fresh profile: the source is the Claude account", backends.current() == "account")
    for b in ("account", "apikey", "local", "custom"):
        p.SetString("Backend", b)
        check(f"current() gives {b}", backends.current() == b)
        # the API key and the local model are the built-in engine: no Claude Code
        check(f"uses_claude_code({b})", backends.uses_claude_code() == (b in ("account", "custom"))
              and backends.uses_claude_code(b) == (b in ("account", "custom")))
    p.SetString("Backend", "nonsense")
    check("an unknown source counts as the account", backends.current() == "account")
    check("labels", [backends.label(b) for b in ("account", "apikey", "local", "custom")] ==
          ["Claude account", "Anthropic API key", "Local model", "Other endpoint"])
    p.SetString("Backend", "local")
    check("label() without argument follows the setting", backends.label() == "Local model")
    src = open(backends.__file__, encoding="utf-8").read()
    check("backends.py imports no Qt", "PySide" not in src and "QtCore" not in src)

    # ---- key files ----
    # Linux and macOS: mode 0600. Windows has no such modes: there the tests are
    # what is true there (the swap works, also for a read-only or an open file).
    def mode(path):
        return stat.S_IMODE(os.stat(path).st_mode)

    for name, get, put in (("cadprompt_api_key", backends.api_key, backends.set_api_key),
                           ("cadprompt_local_key", backends.local_key, backends.set_local_key)):
        path = os.path.join(data, name)
        check(f"{name}: no key at first", get() == "" and not os.path.exists(path))
        put("  made-up-key-" + name + "\n")
        check(f"{name}: file is written", os.path.isfile(path))
        if POSIX:
            check(f"{name}: mode is 0600", mode(path) == 0o600, oct(mode(path)))
        check(f"{name}: read back without spaces", get() == "made-up-key-" + name, repr(get()))
        if POSIX:
            os.chmod(path, 0o644)
            put("second-key-" + name)
            check(f"{name}: an old file that others could read is closed again",
                  mode(path) == 0o600 and get() == "second-key-" + name)
            os.chmod(path, 0o644)                # made by hand, or brought back by a sync tool
            check(f"{name}: a read closes a file that others could read",
                  get() == "second-key-" + name and mode(path) == 0o600, oct(mode(path)))
            os.chmod(path, 0o644)
        else:
            put("between-key-" + name)
            check(f"{name}: a new key takes the place of the old one", get() == "between-key-" + name, repr(get()))
        os.chmod(path, stat.S_IREAD)             # Windows: the read-only mark, which blocks a plain swap
        try:
            put("second-key-" + name)
            error = ""
        except OSError as exc:
            error = repr(exc)
        check(f"{name}: a file marked read-only gets the new key all the same",
              not error and get() == "second-key-" + name, error or repr(get()))
        if POSIX:
            os.chmod(path, 0o644)
        seen = []
        real_replace = os.replace

        def watched_replace(src, dst):       # the moment the new key is on disk
            seen.append((mode(src), open(path).read().strip()))
            real_replace(src, dst)
        os.replace = watched_replace
        with open(path) as already_open:
            try:
                put("third-key-" + name)
                error = ""
            except OSError as exc:
                error = repr(exc)
            finally:
                os.replace = real_replace
            if POSIX:
                check(f"{name}: the new key is never in a file that others can read",
                      seen == [(0o600, "second-key-" + name)] and get() == "third-key-" + name
                      and mode(path) == 0o600, str(seen) + error)
                check(f"{name}: a program that had the old file open does not see the new key",
                      already_open.read().strip() == "second-key-" + name)
            else:
                # Windows does not swap a file that is open: the key is written into the old file
                check(f"{name}: the key is saved while another program has the file open",
                      not error and len(seen) == 1 and get() == "third-key-" + name, str(seen) + error + repr(get()))
        check(f"{name}: no half-written file is left", sorted(n for n in os.listdir(data) if n.startswith(name))
              == [name], str(os.listdir(data)))
        os.chmod(path, stat.S_IREAD)
        put("")
        check(f"{name}: empty text deletes the file, also one marked read-only", not os.path.exists(path) and get() == "")
        put("")
        check(f"{name}: deleting twice is fine", not os.path.exists(path))
        put("   ")
        check(f"{name}: only spaces also means no key", not os.path.exists(path))
        with open(path, "wb") as f:              # not text: a broken file must not stop the settings
            f.write(b"\xff\xfe\x00key")
        check(f"{name}: a file that is not text counts as no key", get() == "")
        put("")
    check("the two keys do not mix", (backends.set_api_key("AAA-made-up"), backends.set_local_key("BBB-made-up"))
          and backends.api_key() == "AAA-made-up" and backends.local_key() == "BBB-made-up")
    check("the key files are in FreeCAD's user data folder, inside the test profile",
          os.path.isfile(os.path.join(data, "cadprompt_api_key")) and inside(data, home), f"{data} {home}")

    # ---- nothing about keys in FreeCAD's parameters ----
    p.SetString("LocalUrl", "http://127.0.0.1:1234/v1")
    App.saveParameter()
    cfg = open(os.path.join(App.getUserConfigDir(), "user.cfg"), encoding="utf-8").read()
    check("user.cfg was written and has our group", "CADprompt" in cfg and "LocalUrl" in cfg)
    check("user.cfg does not hold a key", "AAA-made-up" not in cfg and "BBB-made-up" not in cfg)
    names = [n for (_kind, n, _v) in p.GetContents()]
    check("no parameter is named like a key", not [n for n in names if "key" in n.lower()], str(names))

    # ---- local_config ----
    p.RemString("LocalUrl")
    p.RemString("LocalModel")
    backends.set_local_key("")
    check("local_config defaults", backends.local_config() ==
          {"url": "http://127.0.0.1:1234/v1", "model": "", "key": ""}, str(backends.local_config()))
    p.SetString("LocalUrl", " http://example.invalid:8080/v1/ ")
    p.SetString("LocalModel", " google/gemma-4-e4b ")
    backends.set_local_key("BBB-made-up")
    check("local_config from the settings", backends.local_config() ==
          {"url": "http://example.invalid:8080/v1", "model": "google/gemma-4-e4b", "key": "BBB-made-up"},
          str(backends.local_config()))
    p.SetString("LocalUrl", "")
    check("a cleared address stays empty, so the panel can ask for it", backends.local_config()["url"] == "")

    # ---- engine_config: what the built-in engine gets for a source ----
    p.SetString("LocalUrl", "http://example.invalid:8080/v1")
    backends.set_api_key("AAA-made-up")
    p.RemString("ApiModel")
    p.SetString("Backend", "local")
    check("engine_config local: the OpenAI format, the local address and the local key",
          backends.engine_config() == {"kind": "openai", "url": "http://example.invalid:8080/v1",
                                       "model": "google/gemma-4-e4b", "key": "BBB-made-up"},
          str(backends.engine_config()))
    check("engine_config(apikey): the Anthropic format, the fixed address, the default model, the API key",
          backends.engine_config("apikey") == {"kind": "anthropic", "url": "https://api.anthropic.com",
                                               "model": "claude-opus-5-5", "key": "AAA-made-up"},
          str(backends.engine_config("apikey")))
    p.SetString("Backend", "apikey")
    p.SetString("ApiModel", " claude-haiku-5-5 ")
    check("engine_config follows the source and the model chosen in Settings",
          backends.engine_config()["kind"] == "anthropic" and backends.engine_config()["model"] == "claude-haiku-5-5")
    p.SetString("ApiModel", "")
    check("a cleared API model is the default again", backends.api_model() == backends.API_MODEL == "claude-opus-5-5"
          and backends.API_MODEL in backends.API_MODELS)
    both = json.dumps([backends.engine_config("apikey"), backends.engine_config("local")])
    check("each key is in the config of its own service only",
          both.count("AAA-made-up") == 1 and both.count("BBB-made-up") == 1
          and backends.engine_config("apikey")["key"] == "AAA-made-up"
          and backends.engine_config("local")["key"] == "BBB-made-up")
    for name in ("LocalUrl", "BaseUrl"):
        p.SetString(name, "http://evil.invalid/v1")
    check("no setting moves the Anthropic key to another address",
          backends.engine_config("apikey")["url"] == backends.ANTHROPIC_URL == "https://api.anthropic.com")
    p.SetString("BaseUrl", "")
    check("engine_config is None for the sources that run Claude Code",
          backends.engine_config("account") is None and backends.engine_config("custom") is None)
    check("may_contact(kind, url) is the gate, and it is open for now",
          backends.may_contact("anthropic", backends.ANTHROPIC_URL) is True
          and backends.may_contact("openai", "http://127.0.0.1:1234/v1") is True)
    p.SetString("LocalUrl", "")

    # ---- claude_env ----
    p.SetString("Backend", "account")
    check("claude_env account: sets nothing, removes key, token and endpoint", backends.claude_env() ==
          ({}, ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"]), str(backends.claude_env()))
    p.SetString("Backend", "apikey")
    check("claude_env apikey: nothing, the key never goes to Claude Code", backends.claude_env() == ({}, []),
          str(backends.claude_env()))
    p.SetString("Backend", "custom")
    p.SetString("BaseUrl", "http://localhost:11434")
    p.SetString("AuthToken", "")
    p.SetString("Model", "")
    check("claude_env custom, no model, no token", backends.claude_env() ==
          ({"ANTHROPIC_BASE_URL": "http://localhost:11434", "ANTHROPIC_AUTH_TOKEN": "local"},
           ["ANTHROPIC_API_KEY"]), str(backends.claude_env()))
    p.SetString("AuthToken", "tok")
    p.SetString("Model", "qwen-not-used")
    add, drop = backends.claude_env()
    check("claude_env custom with model and token", drop == ["ANTHROPIC_API_KEY"] and add == {
        "ANTHROPIC_BASE_URL": "http://localhost:11434", "ANTHROPIC_AUTH_TOKEN": "tok",
        "ANTHROPIC_MODEL": "qwen-not-used", "ANTHROPIC_DEFAULT_OPUS_MODEL": "qwen-not-used",
        "ANTHROPIC_DEFAULT_SONNET_MODEL": "qwen-not-used",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": "qwen-not-used"}, str(add))
    p.SetString("Backend", "local")
    check("claude_env local: nothing, Claude Code is not used", backends.claude_env() == ({}, []))

    # ---- runner: the same thing applied to a process environment ----
    from cadprompt import runner
    os.environ["ANTHROPIC_API_KEY"] = "shell-key-must-not-leak"
    os.environ["ANTHROPIC_AUTH_TOKEN"] = "shell-token-must-not-leak"
    os.environ["ANTHROPIC_BASE_URL"] = "http://shell.invalid"
    p.SetString("Backend", "custom")
    check("runner.backend_env() still gives the custom endpoint", runner.backend_env().get(
        "ANTHROPIC_BASE_URL") == "http://localhost:11434")
    pe = runner.claude_environment("/nowhere/bin/claude", {"FREECAD_MCP_TOKEN": "t"})
    check("custom process: endpoint from Settings, shell key removed",
          pe.value("ANTHROPIC_BASE_URL") == "http://localhost:11434" and pe.value("ANTHROPIC_AUTH_TOKEN") == "tok"
          and not pe.contains("ANTHROPIC_API_KEY") and pe.value("FREECAD_MCP_TOKEN") == "t")
    p.SetString("Backend", "account")
    check("runner.backend_env() is {} for the account", runner.backend_env() == {})
    pe = runner.claude_environment("/nowhere/bin/claude", {"FREECAD_MCP_TOKEN": "t"})
    check("account process: shell key, token and endpoint are all removed",
          not pe.contains("ANTHROPIC_API_KEY") and not pe.contains("ANTHROPIC_AUTH_TOKEN")
          and not pe.contains("ANTHROPIC_BASE_URL"))
    check("account process: PATH starts with claude's folder, the bridge token is passed",
          pe.value("PATH").split(os.pathsep)[0] == "/nowhere/bin" and pe.value("FREECAD_MCP_TOKEN") == "t")
    check("account process: a key handed in by the caller is removed too",
          not runner.claude_environment("/nowhere/bin/claude", {"ANTHROPIC_API_KEY": "x"}).contains(
              "ANTHROPIC_API_KEY"))
    p.SetString("Backend", "apikey")
    pe = runner.claude_environment("/nowhere/bin/claude", {})
    check("apikey: a program environment never gets the saved key (the engine sends it itself)",
          "AAA-made-up" not in [pe.value(k) for k in pe.keys()])
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"):
        del os.environ[k]

    # ---- a started program gets nothing that belongs to the FreeCAD bundle ----
    # An AppImage, the macOS app and a Windows install bring their own Python. Their start
    # scripts export PYTHONHOME and more; with that, no other Python starts.
    roots = paths.bundle_roots()
    print("FreeCAD's own bundle:", roots or "none (a FreeCAD of the system)")
    check("bundle_roots: none, or the one folder that holds this FreeCAD",
          roots == [] or (len(roots) == 1 and os.path.isdir(roots[0])
                          and inside(App.getHomePath(), roots[0])), str(roots))
    bundle = os.path.join(home, "My FreeCAD Büro", "FreeCAD.AppDir")    # a space and a letter that is not ASCII
    os.makedirs(os.path.join(bundle, "usr", "bin"))
    sep, usr = os.pathsep, os.path.join(bundle, "usr")
    system_bin = os.path.dirname(os.path.abspath(sys.executable))
    shell = {"PYTHONHOME": usr, "PYTHONPATH": sep.join([os.path.join(usr, "lib"), os.path.join(home, "my-modules")]),
             "LD_LIBRARY_PATH": os.path.join(usr, "lib"), "SSL_CERT_FILE": os.path.join(usr, "ssl", "cacert.pem"),
             "GIT_SSL_CAINFO": os.path.join(usr, "ssl", "cacert.pem"), "PREFIX": usr + os.sep,
             "PATH": sep.join([os.path.join(usr, "bin"), system_bin, usr, os.path.join(home, "bin")]),
             "XCURSOR_PATH": sep.join(["~/.icons", os.path.join(usr, "share", "icons")]),
             "FONTCONFIG_PATH": "/etc/fonts", "QT_QPA_PLATFORM": "xcb", "HOME_PAGE": "https://example.invalid/a:b",
             "NEIGHBOUR": bundle + "-other", "EMPTY": "", "LANG": "de_LU.UTF-8"}
    got = paths.child_environment(shell, roots=[bundle])
    check("child_environment: PYTHONHOME, the library path, the certificate files and PREFIX of the bundle are gone",
          not [k for k in ("PYTHONHOME", "LD_LIBRARY_PATH", "SSL_CERT_FILE", "GIT_SSL_CAINFO", "PREFIX") if k in got],
          str(sorted(got)))
    check("child_environment: a list loses only its entries inside the bundle",
          got.get("PATH") == sep.join([system_bin, os.path.join(home, "bin")])
          and got.get("PYTHONPATH") == os.path.join(home, "my-modules")
          and got.get("XCURSOR_PATH") == "~/.icons", str({k: got.get(k) for k in ("PATH", "PYTHONPATH", "XCURSOR_PATH")}))
    check("child_environment: everything else stays as it was, also a folder with a similar name",
          {k: got.get(k) for k in ("FONTCONFIG_PATH", "QT_QPA_PLATFORM", "HOME_PAGE", "NEIGHBOUR", "EMPTY", "LANG")}
          == {k: shell[k] for k in ("FONTCONFIG_PATH", "QT_QPA_PLATFORM", "HOME_PAGE", "NEIGHBOUR", "EMPTY", "LANG")},
          str(got))
    check("child_environment: the dict that was handed in is not changed", shell["PYTHONHOME"] == usr and "PATH" in shell)
    check("child_environment: without a bundle (a FreeCAD of the system) nothing is taken out",
          paths.child_environment(shell, roots=[]) == shell)
    if os.name == "nt":
        mixed = {"Path": sep.join([os.path.join(usr.upper(), "bin"), usr.replace(os.sep, "/") + "/bin", system_bin])}
        check("child_environment, Windows: upper or lower case and / or \\ name the same folder",
              paths.child_environment(mixed, roots=[bundle]) == {"Path": system_bin},
              str(paths.child_environment(mixed, roots=[bundle])))
    with open(os.path.join(bundle, "AppRun"), "w", encoding="utf-8") as f:
        f.write("#!/bin/sh\n")
    appimage = dict(shell, APPDIR=bundle, APPIMAGE=os.path.join(home, "FreeCAD.AppImage"), ARGV0="FreeCAD.AppImage",
                    OWD=home)
    got = paths.child_environment(appimage, roots=[bundle])
    check("child_environment, AppImage: APPDIR, APPIMAGE, ARGV0 and OWD describe FreeCAD and are gone too",
          not [k for k in ("APPDIR", "APPIMAGE", "ARGV0", "OWD", "PYTHONHOME") if k in got] and got.get("LANG") == shell["LANG"],
          str(sorted(got)))
    # the same through the real function, with the real environment of this FreeCAD
    real_roots, paths.bundle_roots = paths.bundle_roots, lambda: roots + [bundle]
    os.environ["PYTHONPATH"] = os.path.join(usr, "lib")
    os.environ["CADPROMPT_TEST_BUNDLE_FILE"] = os.path.join(usr, "ssl", "cacert.pem")
    os.environ["CADPROMPT_TEST_KEPT"] = "kept"
    try:
        pe = runner.system_environment()
        names = [k for k in pe.keys() if bundle in pe.value(k)]
        check("runner.system_environment: nothing in it points into the bundle, the rest is there",
              not names and not pe.contains("PYTHONPATH") and not pe.contains("CADPROMPT_TEST_BUNDLE_FILE")
              and pe.value("CADPROMPT_TEST_KEPT") == "kept" and pe.contains("PATH"), str(names))
        check("runner.system_environment: FreeCAD's own PYTHONHOME is not handed on",
              not roots or not pe.contains("PYTHONHOME"), pe.value("PYTHONHOME"))
        pe = runner.claude_environment(os.path.join(home, "bin", "claude"), {"FREECAD_MCP_TOKEN": "t"})
        check("claude process: the same clean environment, with claude's folder first in PATH",
              not [k for k in pe.keys() if bundle in pe.value(k)] and not pe.contains("PYTHONPATH")
              and pe.value("PATH").split(os.pathsep)[0] == os.path.join(home, "bin")
              and pe.value("FREECAD_MCP_TOKEN") == "t", pe.value("PATH")[:200])
    finally:
        paths.bundle_roots = real_roots
        for k in ("PYTHONPATH", "CADPROMPT_TEST_BUNDLE_FILE", "CADPROMPT_TEST_KEPT"):
            del os.environ[k]

    # ---- claude.cmd on Windows: the command line for cmd.exe ----
    line = runner._cmd_line(r"C:\Users\Zoë Müller\AppData\Roaming\npm\claude.cmd", ["-p", "--model", "a b", "x&y"])
    check("a .cmd file: one pair of quotes around the whole line, every word in quotes of its own",
          line == '/d /s /c ""C:\\Users\\Zoë Müller\\AppData\\Roaming\\npm\\claude.cmd" "-p" "--model" "a b" "x&y""', line)
    check("only a .cmd or .bat file on Windows goes through cmd.exe",
          runner._is_script("C:/x/claude.CMD") == paths.WIN and not runner._is_script("C:/x/claude.exe")
          and not runner._is_script("/usr/bin/claude"))

    # ---- project files: the same text on every system ----
    from cadprompt import projects
    proj = os.path.join(home, "Projekt Größe Ø80")
    os.makedirs(proj)
    check("mode and session: nothing saved yet", projects.get_mode(proj) == "build" and projects.get_session(proj) is None)
    projects.set_mode(proj, "teach")
    projects.set_session(proj, "session-ü-1")
    check("mode and session are saved and read back, in a folder with a space and letters that are not ASCII",
          projects.get_mode(proj) == "teach" and projects.get_session(proj) == "session-ü-1")
    with open(os.path.join(proj, ".cadprompt", "session"), "rb") as f:
        check("the session file is UTF-8 on every system", f.read().rstrip() == "session-ü-1".encode("utf-8"))
    with open(os.path.join(proj, ".cadprompt", "mode"), "wb") as f:
        f.write(b"teach\r\n")                  # a file that Windows git or an editor gave CRLF line ends
    check("a mode file with Windows line ends reads the same", projects.get_mode(proj) == "teach")
    projects.set_session(proj, None)
    check("an empty session id removes the file", projects.get_session(proj) is None)
    check("slug: a folder name that every system takes",
          projects.slug("Flansch Ø80 / größer") == "flansch-80-gr-er" and projects.slug("///") == "untitled",
          projects.slug("Flansch Ø80 / größer"))

    # ---- list_models ----
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Fake)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % server.server_address[1]
    # This computer has a proxy in its settings, as in many schools, and no exception for local
    # addresses. All checks below run like that: the fake server must still be asked directly.
    proxy = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Proxy)
    threading.Thread(target=proxy.serve_forever, daemon=True).start()
    names = ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "no_proxy", "NO_PROXY")
    shell = {k: os.environ.pop(k) for k in names if k in os.environ}
    for k in names[:4]:
        os.environ[k] = "http://127.0.0.1:%d" % proxy.server_address[1]
    try:
        got = backends.list_models(base + "/v1", key="LOCAL-KEY-made-up")
    except OSError as exc:
        got = repr(exc)
    check("list_models: no proxy is used; the key goes to the typed address and to no other host",
          got == ["google/gemma-4-e4b", "other-model"] and not _Proxy.seen
          and _Fake.seen[-1] == ("/v1/models", "Bearer LOCAL-KEY-made-up"), f"{got} proxy saw {_Proxy.seen}")
    got = backends.list_models(base + "/v1")
    check("list_models: ids from the fake server, each once", got == ["google/gemma-4-e4b", "other-model"], str(got))
    check("list_models: no Authorization header without a key", _Fake.seen[-1] == ("/v1/models", None),
          str(_Fake.seen[-1]))
    backends.list_models(base + "/v1/", key="abc")
    check("list_models: Bearer header with a key, a trailing slash is fine",
          _Fake.seen[-1] == ("/v1/models", "Bearer abc"), str(_Fake.seen[-1]))
    check("list_models: {'models': [{'name': ...}]} is read too",
          backends.list_models(base + "/ollama") == ["llama3:8b"])
    check("list_models: the right key opens a locked server",
          backends.list_models(base + "/locked", "right-key") == ["paid-model"])
    check("list_models: a refused key is an OSError (HTTP 401)",
          raises(OSError, lambda: backends.list_models(base + "/locked", "wrong")) == "HTTPError")
    check("list_models: an answer that is no model list is a ValueError",
          raises(ValueError, lambda: backends.list_models(base)) == "ValueError")
    check("list_models: an answer that is no JSON is a ValueError",
          bool(raises(ValueError, lambda: backends.list_models(base + "/text"))))
    check("list_models: file:// and empty addresses are refused",
          raises(ValueError, lambda: backends.list_models("file:///etc")) == "ValueError"
          and raises(ValueError, lambda: backends.list_models("")) == "ValueError")
    got = backends.list_models(base + "/anthropic", "sk-ant-made-up", kind="anthropic")
    check("list_models anthropic: GET /v1/models, the ids of the list",
          got == ["claude-fake-1", "claude-fake-2"] and _Fake.seen[-1][0] == "/anthropic/v1/models?limit=100",
          f"{got} {_Fake.seen[-1]}")
    check("list_models anthropic: the key is in x-api-key with the API version, never a Bearer header",
          _Fake.seen[-1][1] is None and _Fake.claude[-1] == ("sk-ant-made-up", backends.ANTHROPIC_VERSION)
          and backends.ANTHROPIC_VERSION == local_agent.AnthropicWire.version, f"{_Fake.seen[-1]} {_Fake.claude}")
    check("list_models anthropic: a refused key is an OSError (HTTP 401)",
          raises(OSError, lambda: backends.list_models(base + "/anthropic", "wrong", kind="anthropic")) == "HTTPError")
    other = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Fake)
    threading.Thread(target=other.serve_forever, daemon=True).start()
    port = other.server_address[1]
    for code in (301, 302, 303, 307, 308):
        n = len(_Fake.seen)
        r = raises(OSError, lambda: backends.list_models(base + "/redirect/%d/%d" % (code, port), "SECRET-KEY"))
        check(f"list_models: a redirect ({code}) is an error, the key does not go to the other host",
              r == "HTTPError" and _Fake.seen[n:] == [("/redirect/%d/%d/models" % (code, port), "Bearer SECRET-KEY")],
              f"{r} {_Fake.seen[n:]}")
    other.shutdown()
    other.server_close()
    check("same_server: the same host and port, any path",
          backends.same_server("http://127.0.0.1:1234/v1", " HTTP://127.0.0.1:1234/other/ ")
          and backends.same_server("https://openrouter.ai/api/v1", "https://OpenRouter.ai/api"))
    check("same_server: another host, port or scheme is another server",
          not backends.same_server("https://openrouter.ai/api/v1", "http://192.168.1.50:1234/v1")
          and not backends.same_server("http://127.0.0.1:1234/v1", "http://127.0.0.1:11434/v1")
          and not backends.same_server("https://openrouter.ai/api/v1", "http://openrouter.ai/api/v1")
          and not backends.same_server("https://openrouter.ai/api/v1", ""))
    check("same_server: an address without http:// still has its own host and port",
          backends.same_server("localhost:1234/v1", " LocalHost:1234/other/ ")
          and not backends.same_server("api.openai.com/v1", "openrouter.ai/api/v1")
          and not backends.same_server("192.168.1.20:1234/v1", "192.168.1.21:1234/v1")
          and not backends.same_server("localhost:1234/v1", "localhost:11434/v1")
          and not backends.same_server("localhost:1234/v1", "http://localhost:1234/v1"))
    check("same_server: a half-typed address is no server, and no error",
          not backends.same_server("http://[", "http://[") and not backends.same_server("http://[", ""))
    t0 = time.time()
    r = raises(OSError, lambda: backends.list_models(base + "/slow", timeout=1))
    check("list_models: a server that does not answer in time is an OSError", bool(r) and time.time() - t0 < 3,
          f"{r} after {time.time() - t0:.1f} s")
    server.shutdown()
    server.server_close()
    t0 = time.time()
    r = raises(OSError, lambda: backends.list_models(base + "/v1"))
    check("list_models: a dead port is an OSError, at once", bool(r) and time.time() - t0 < 5,
          f"{r} after {time.time() - t0:.1f} s")
    check("list_models: in all these checks nothing reached the proxy", not _Proxy.seen, str(_Proxy.seen))
    for k in names:
        os.environ.pop(k, None)
    os.environ.update(shell)
    proxy.shutdown()
    proxy.server_close()


try:
    main()
except Exception:
    traceback.print_exc()
    FAILED.append("the test script itself")
print(f"\n{COUNT[0] - len([f for f in FAILED if f != 'the test script itself'])} of {COUNT[0]} checks passed"
      + (", FAILED: " + "; ".join(FAILED) if FAILED else ", all good"), flush=True)
os._exit(1 if FAILED else 0)
