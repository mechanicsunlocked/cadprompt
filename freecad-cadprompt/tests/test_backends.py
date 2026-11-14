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

sys.dont_write_bytecode = True       # no __pycache__ in the repository
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

FAILED = []
COUNT = [0]


def check(name, ok, detail=""):
    COUNT[0] += 1
    if not ok:
        FAILED.append(name)
    print(("ok    " if ok else "FAIL  ") + name + (f"   [{detail}]" if detail and not ok else ""), flush=True)


class _Fake(http.server.BaseHTTPRequestHandler):
    """A pretend model server: /v1 answers like LM Studio, the other paths misbehave."""
    seen = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        _Fake.seen.append((self.path, self.headers.get("Authorization")))
        if self.path == "/v1/models":
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
    from cadprompt import backends, paths

    home = os.environ.get("FREECAD_USER_HOME", "")
    data = App.getUserAppDataDir()
    print("FreeCAD user data:", data)
    p = paths.params()

    # ---- which source ----
    check("fresh profile: the source is the Claude account", backends.current() == "account")
    for b in ("account", "apikey", "local", "custom"):
        p.SetString("Backend", b)
        check(f"current() gives {b}", backends.current() == b)
        check(f"uses_claude_code({b})", backends.uses_claude_code() == (b != "local")
              and backends.uses_claude_code(b) == (b != "local"))
    p.SetString("Backend", "nonsense")
    check("an unknown source counts as the account", backends.current() == "account")
    check("labels", [backends.label(b) for b in ("account", "apikey", "local", "custom")] ==
          ["Claude account", "Anthropic API key", "Local model", "Other endpoint"])
    p.SetString("Backend", "local")
    check("label() without argument follows the setting", backends.label() == "Local model")
    src = open(backends.__file__, encoding="utf-8").read()
    check("backends.py imports no Qt", "PySide" not in src and "QtCore" not in src)

    # ---- key files ----
    for name, get, put in (("cadprompt_api_key", backends.api_key, backends.set_api_key),
                           ("cadprompt_local_key", backends.local_key, backends.set_local_key)):
        path = os.path.join(data, name)
        check(f"{name}: no key at first", get() == "" and not os.path.exists(path))
        put("  made-up-key-" + name + "\n")
        check(f"{name}: file is written", os.path.isfile(path))
        check(f"{name}: mode is 0600", stat.S_IMODE(os.stat(path).st_mode) == 0o600,
              oct(stat.S_IMODE(os.stat(path).st_mode)))
        check(f"{name}: read back without spaces", get() == "made-up-key-" + name, repr(get()))
        os.chmod(path, 0o644)
        put("second-key-" + name)
        check(f"{name}: an old file that others could read is closed again",
              stat.S_IMODE(os.stat(path).st_mode) == 0o600 and get() == "second-key-" + name)
        put("")
        check(f"{name}: empty text deletes the file", not os.path.exists(path) and get() == "")
        put("")
        check(f"{name}: deleting twice is fine", not os.path.exists(path))
        put("   ")
        check(f"{name}: only spaces also means no key", not os.path.exists(path))
    check("the two keys do not mix", (backends.set_api_key("AAA-made-up"), backends.set_local_key("BBB-made-up"))
          and backends.api_key() == "AAA-made-up" and backends.local_key() == "BBB-made-up")
    check("the key files are in FreeCAD's user data folder, inside the test profile",
          os.path.isfile(os.path.join(data, "cadprompt_api_key")) and (not home or data.startswith(home)))

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

    # ---- claude_env ----
    p.SetString("Backend", "account")
    check("claude_env account: sets nothing, removes key, token and endpoint", backends.claude_env() ==
          ({}, ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"]), str(backends.claude_env()))
    p.SetString("Backend", "apikey")
    check("claude_env apikey: sets the saved key, removes token and endpoint", backends.claude_env() ==
          ({"ANTHROPIC_API_KEY": "AAA-made-up"}, ["ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"]),
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
    check("apikey process: the saved key wins over the shell's, token and endpoint removed",
          pe.value("ANTHROPIC_API_KEY") == "AAA-made-up" and not pe.contains("ANTHROPIC_AUTH_TOKEN")
          and not pe.contains("ANTHROPIC_BASE_URL"))
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"):
        del os.environ[k]

    # ---- list_models ----
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Fake)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % server.server_address[1]
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


try:
    main()
except Exception:
    traceback.print_exc()
    FAILED.append("the test script itself")
print(f"\n{COUNT[0] - len([f for f in FAILED if f != 'the test script itself'])} of {COUNT[0]} checks passed"
      + (", FAILED: " + "; ".join(FAILED) if FAILED else ", all good"), flush=True)
os._exit(1 if FAILED else 0)
