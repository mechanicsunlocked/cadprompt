"""The built-in engine: a local model or an Anthropic API key, without Claude Code
and without the bridge.

LocalRun has the same signals as runner.ClaudeRun, so the panel can use
either. The chat loop (local_agent) talks HTTP in a worker thread. FreeCAD is
not thread-safe, so every tool call is handed to the main thread and the
worker waits for the result. The conversation is kept in the project, in
.cadprompt/local-chat.json, and goes on with the next start(). It has one
shape for all services (see local_agent), so the source can change in the
middle of a project. Images are kept as paths in the project, never as data.
"""

import base64
import json
import os
import re
import traceback

import FreeCAD as App
from PySide import QtCore, QtGui

from . import approval, backends, local_agent, local_prompt, local_tools, main_thread, projects

CHAT_FILE = "local-chat.json"
MAX_STORED = 400                   # messages kept in the file; older requests are dropped whole
STOPPED = {"role": "assistant", "content": "*Stopped.*"}
CHANGING = local_prompt.CHANGING   # tools that can change the document
SOURCES = {"openai": "local", "anthropic": "apikey"}     # wire format -> the source, for approval.request
NOT_SAVED = ("*The model file was not saved, because this request did not end well. Look at the "
             "model in FreeCAD: save it yourself if it is good, or undo the steps.*")
MAX_IMAGES = local_agent.MAX_IMAGES
IMAGE_SIDE = 1568                  # longest side of an image that is sent, in pixels
IMAGE_PIXELS = 64 * 1024 * 1024    # a larger image is refused: Qt needs 4 bytes for each pixel to open it
IMAGE_BYTES = 3_500_000            # a larger PNG goes as JPEG: the services take about 5 MB
DOC_CLOSED = ('ERROR: the document "%s" was closed during this request. Nothing more can be done '
              "in it. Stop and tell the user.")

_COMMAND = re.compile(r"^/([A-Za-z0-9_-]+)(?:\s+(.*))?$", re.S)


# ---- the stored conversation -------------------------------------------------
def _chat_path(project):
    return os.path.join(project, ".cadprompt", CHAT_FILE)


def _load(project):
    try:
        with open(_chat_path(project), encoding="utf-8") as f:
            messages = json.load(f).get("messages")
    except (OSError, ValueError, AttributeError):
        return []
    if not isinstance(messages, list):
        return []
    return [m for m in messages if isinstance(m, dict) and m.get("role")]


def _save(project, messages):
    if len(messages) > MAX_STORED:
        starts = [i for i, m in enumerate(messages) if m.get("role") == "user" and not m.get("auto")]
        late = [i for i in starts if len(messages) - i <= MAX_STORED]
        messages = messages[late[0] if late else starts[-1] if starts else 0:]
    path = _chat_path(project)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        json.dump({"version": 1, "messages": messages}, f, indent=1)
        f.write("\n")
    os.replace(path + ".tmp", path)    # never a half-written conversation


def _tool_detail(args):
    """A few words about a tool call, for the chat view."""
    if not isinstance(args, dict):
        return ""
    for key in ("path", "sheet", "obj_name"):
        v = args.get(key)
        if isinstance(v, str) and v:
            return v
    code = args.get("code")
    if isinstance(code, str) and code.strip():
        first = code.strip().splitlines()[0]
        return first[:60] + ("…" if len(first) > 60 else "")
    return ""


def history(project, limit=200):
    """(kind, text) entries of the stored conversation: user, claude, tool."""
    out = []
    for m in _load(project):
        role, content = m.get("role"), m.get("content")
        content = content if isinstance(content, str) else ""
        if role == "user" and not m.get("auto"):
            out.append(("user", m.get("shown") or content))
            if m.get("images"):
                out.append(("note", images_note(len(m["images"]))))
        elif role == "assistant":
            if content.strip():
                out.append(("claude", content))
            for call in m.get("tool_calls") or []:
                fn = call.get("function") or {}
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except ValueError:
                    args = {}
                out.append(("tool", f"`{fn.get('name') or 'tool'}` {_tool_detail(args)}".strip()))
    return out[-limit:]


def clear(project):
    """Start a new chat: forget the conversation and the variables of run_python."""
    try:
        os.remove(_chat_path(project))
    except OSError:
        pass
    local_tools.reset()


# ---- images ------------------------------------------------------------------
class ImageError(ValueError):
    """A file that cannot go with a message as an image. The text is for the user;
    `path` is the file as it was given."""

    def __init__(self, text, path=""):
        super().__init__(text)
        self.path = path


def images_note(n):
    return "with %d image%s" % (n, "" if n == 1 else "s")


def _format(reader):
    return bytes(reader.format().data()).decode("ascii", "replace").lower()


def _too_large(reader):
    """Text for an image with too many pixels to open, else "".

    Qt 6 refuses to open such an image (256 MB), Qt 5 takes the memory. A JPEG
    has no limit: Qt opens it at 1/2, 1/4 or 1/8 of its size (see encode_image).
    """
    size = reader.size()
    if _format(reader) in ("jpeg", "jpg") or size.width() * size.height() <= IMAGE_PIXELS:
        return ""
    return ("is too large: %d x %d pixels, and the limit is %d million pixels. Make it smaller "
            "in an image program." % (size.width(), size.height(), IMAGE_PIXELS // 1_000_000))


def project_images(project, files):
    """The images for one message, as paths in the project ("refs/photo.png").

    A file outside the project is copied to refs/ first, so the conversation
    never points out of its project. Raises ImageError for a file that is not
    an image, for an image that is too large to open, and for more than
    MAX_IMAGES files.
    """
    out, root = [], os.path.realpath(project)
    for f in files or []:
        path = f if os.path.isabs(f) else os.path.join(project, f)
        reader = QtGui.QImageReader(path)
        if not os.path.isfile(path) or not reader.canRead():
            raise ImageError("`%s` is not an image that CADprompt can read (PNG or JPEG, for "
                             "example). It was not sent." % os.path.basename(path), f)
        problem = _too_large(reader)
        if problem:
            raise ImageError("`%s` %s It was not sent." % (os.path.basename(path), problem), f)
        try:
            rel = os.path.relpath(os.path.realpath(path), root)
        except ValueError:
            rel = ".."                       # Windows: the file is on another drive
        if rel.startswith(".."):
            rel = os.path.join("refs", projects.add_refs(project, [path])[0])
        rel = rel.replace(os.sep, "/")
        if rel not in out:
            out.append(rel)
    if len(out) > MAX_IMAGES:
        raise ImageError("At most %d images can go with one message." % MAX_IMAGES)
    return out


def _project_file(project, rel):
    """The file of a stored image path. The stored text cannot point out of the project."""
    parts = str(rel).split("/")
    # Look at the text first. The chat file can come from another person, and on Windows
    # realpath opens the path: for a name such as \\host\share\x.png that is a login at that host.
    if any(p in ("", ".", "..") or os.sep in p or os.path.splitdrive(p)[0] for p in parts):
        raise ImageError("The image %s is not in the project." % rel, rel)
    root = os.path.realpath(project)
    path = os.path.realpath(os.path.join(root, *parts))
    if not os.path.normcase(path).startswith(os.path.normcase(root) + os.sep):
        raise ImageError("The image %s is not in the project." % rel, rel)
    return path


def _encoded(img, kind, quality=-1):
    data = QtCore.QByteArray()
    buf = QtCore.QBuffer(data)
    buf.open(QtCore.QIODevice.WriteOnly)
    ok = img.save(buf, kind, quality)
    buf.close()
    return bytes(data.data()) if ok else b""


def _no_text(img):
    """The same picture without the text of its file (author, comment, XMP block).

    Qt keeps that text with the pixels and writes it into the data that is
    sent. QImage cannot remove a text key, only empty it, and a key can be
    text of the file too: so the pixels go into a new image.
    """
    pixels = bytes(img.constBits())          # bytes: every PySide takes them
    clean = QtGui.QImage(pixels, img.width(), img.height(), img.bytesPerLine(), img.format()).copy()
    if clean.isNull():
        return clean
    clean.setColorTable(img.colorTable())    # a PNG with a palette
    clean.setColorSpace(img.colorSpace())    # a photo in Display P3 keeps its colours
    return clean


def encode_image(path):
    """(media type, base64 text) of an image file, ready to send.

    The longest side is at most IMAGE_SIDE pixels: more does not help a model
    and costs tokens. A photo stays JPEG, everything else becomes PNG. Only
    the pixels are sent, not the text that the file has about its author or
    its place. Runs in the worker thread: QImage is safe there (QPixmap is not).
    """
    reader = QtGui.QImageReader(path)
    reader.setAutoTransform(True)            # a phone photo is stored turned
    kind = _format(reader)
    problem = _too_large(reader)             # the file can change after project_images saw it
    if problem:
        raise ImageError("The image %s %s" % (os.path.basename(path), problem), path)
    size = reader.size()
    if max(size.width(), size.height()) > IMAGE_SIDE:
        # Qt then opens a JPEG at a part of its size: a 108-megapixel photo needs 30 MB, not 430
        reader.setScaledSize(size.scaled(IMAGE_SIDE, IMAGE_SIDE, QtCore.Qt.KeepAspectRatio))
    img = reader.read()
    if img.isNull():
        raise ImageError("The image %s could not be read." % os.path.basename(path), path)
    if max(img.width(), img.height()) > IMAGE_SIDE:      # a format that does not tell its size
        img = img.scaled(IMAGE_SIDE, IMAGE_SIDE, QtCore.Qt.KeepAspectRatio, QtCore.Qt.SmoothTransformation)
    img = _no_text(img)
    if img.isNull():
        raise ImageError("The image %s could not be made ready to send." % os.path.basename(path), path)
    photo = kind in ("jpeg", "jpg") and not img.hasAlphaChannel()
    data = _encoded(img, "JPEG", 85) if photo else b""
    if not data:                             # no photo, or a Qt that cannot write JPEG
        data, photo = _encoded(img, "PNG"), False
    if not photo and len(data) > IMAGE_BYTES:
        flat = QtGui.QImage(img.size(), QtGui.QImage.Format_RGB32)
        flat.fill(QtCore.Qt.white)           # JPEG has no transparency: white paper behind a drawing
        painter = QtGui.QPainter(flat)
        painter.drawImage(0, 0, img)
        painter.end()
        small = _encoded(flat, "JPEG", 85)
        if small:
            data, photo = small, True
    if not data:
        raise ImageError("The image %s could not be made ready to send." % os.path.basename(path), path)
    return ("image/jpeg" if photo else "image/png"), base64.b64encode(data).decode("ascii")


# ---- slash commands ----------------------------------------------------------
def expand(project, prompt):
    """A typed "/name args" becomes the text of .claude/commands/name.md.

    Returns (text for the model, mode to set or None). An unknown command is
    sent as it was typed.
    """
    m = _COMMAND.match(prompt.strip())
    if not m:
        return prompt, None
    name, args = m.group(1), (m.group(2) or "").strip()
    mode = name if name in ("build", "teach") else None
    try:
        with open(os.path.join(project, ".claude", "commands", name + ".md"), encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return prompt, mode
    if text.startswith("---"):                      # YAML front matter is for Claude Code
        end = re.search(r"^---\s*$", text[3:], re.M)
        if end:
            text = text[3 + end.end():]
    if "$ARGUMENTS" in text:
        text = text.replace("$ARGUMENTS", args)
    elif args:
        text += "\n" + args
    hint = local_prompt.COMMAND_HINTS.get(name)
    if hint:
        text = text.rstrip() + "\n" + hint
    return text.strip() or prompt, mode


# ---- worker thread -----------------------------------------------------------
_Call = main_thread.Call     # one request of the worker to the main thread


class _Worker(QtCore.QThread):
    event = QtCore.Signal(int, str, object)     # run number, kind, data (see local_agent.Agent)
    need_tool = QtCore.Signal(int, object)      # run number, _Call
    need_leave = QtCore.Signal(int, object)     # run number, _Call(kind, url): may this request go out?
    done = QtCore.Signal(int, object)           # run number, the dict for LocalRun.finished

    def __init__(self, number, project, config, system, messages, text, shown, images):
        super().__init__()     # no parent: the thread must be able to outlive a closed panel
        self.number, self.ask_text, self.shown, self.images = number, text, shown, images
        extra = {k: config[k] for k in ("max_steps", "max_tokens", "temperature", "timeout", "connect_timeout",
                                        "context_chars") if config.get(k) is not None}
        self.agent = local_agent.Agent(
            config.get("url", ""), config.get("model", ""), self._tool, key=config.get("key", ""),
            system=system, tools=local_prompt.TOOLS, messages=messages,
            on_event=lambda kind, data: self.event.emit(self.number, kind, data),
            kind=config.get("kind") or "openai", may_contact=self._may_contact,
            load_image=lambda rel: encode_image(_project_file(project, rel)), **extra)

    def run(self):
        try:
            res = {"is_error": False, "result": self.agent.ask(self.ask_text, self.shown, self.images)}
        except local_agent.Stopped:
            return                               # stop() already told the panel
        except local_agent.NoVision as exc:
            res = {"is_error": True, "result": str(exc), "no_vision": True}
        except local_agent.AgentError as exc:
            res = {"is_error": True, "result": str(exc)}
        except Exception:
            res = {"is_error": True, "result": "The built-in engine failed:\n" + traceback.format_exc(limit=-4)}
        if self.agent.incomplete and not res["is_error"]:
            res["incomplete"] = True             # the step limit, a cut answer, or a problem not repaired
        if not self.agent.stopped():
            self.done.emit(self.number, res)

    def _tool(self, name, args):
        """Called in this thread: let the main thread run the tool, wait for it.

        The wait looks at the stop flag, so it cannot hang when the run was
        stopped or the panel is gone and nobody will ever answer.
        """
        return self._ask_main(self.need_tool, _Call(name, args))

    def _may_contact(self, kind, url):
        """Called in this thread before every request. The main thread decides
        (backends.may_contact), so a question to the user can be asked there."""
        return self._ask_main(self.need_leave, _Call(kind, url)) is True

    def _ask_main(self, signal, call):
        signal.emit(self.number, call)
        if not main_thread.wait(call, self.agent.stopped):
            raise local_agent.Stopped()
        return call.result


_threads = set()        # keeps running workers alive, also after their LocalRun is gone
_quit_hooked = False


def _reap():
    for w in [w for w in _threads if w.isFinished()]:
        _threads.discard(w)


def _shutdown():
    """FreeCAD is closing: end the workers, so no thread is destroyed while it runs."""
    for w in list(_threads):
        w.agent.stop()
    for w in list(_threads):
        w.wait(3000)
    _reap()


# ---- the engine --------------------------------------------------------------
class LocalRun(QtCore.QObject):
    text = QtCore.Signal(str)              # a block of the model's answer (markdown)
    tool = QtCore.Signal(str, str)         # tool name, short detail
    tool_error = QtCore.Signal(str)        # a tool result that failed
    # {"is_error", "result"}; plus "stopped" after stop(); plus "incomplete" when the work did not end well
    finished = QtCore.Signal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._number = 0          # counts runs; events of an older run are dropped
        self._worker = None
        self._project = None
        self._safe = None         # the last consistent copy of the conversation
        self._ran = []            # (name, args, result) of the tools done since that copy
        self._doc = None          # name of the document this run works on
        self._backed = set()      # model files that have their backup copy for this request
        self._source = "local"    # the source of this run, as approval.request wants it
        self._changed = False
        self._in_tool = False
        live = self._live = []    # the running worker, shared with the hook below
        # the panel can be closed in the middle of a run: stop the worker then
        self.destroyed.connect(lambda *_: [w.agent.stop() for w in live])

    def running(self):
        return self._worker is not None

    def start(self, project, prompt, config, images=None):
        """Send one message. `config` is backends.engine_config(); `images` are
        files that go with the message (see project_images, which can raise ImageError)."""
        global _quit_hooked
        if self.running():
            return
        images = project_images(project, images)
        _reap()
        app = QtCore.QCoreApplication.instance()
        if app is not None and not _quit_hooked:
            app.aboutToQuit.connect(_shutdown)
            _quit_hooked = True

        text, mode = expand(project, prompt)
        if mode:
            projects.set_mode(project, mode)
        _open_model(project)
        self._doc = _active_name()
        local_tools.begin_request()
        messages = _load(project)
        self._number += 1
        self._project, self._changed = project, False
        self._source = SOURCES.get(config.get("kind") or "openai", "local")
        self._safe = messages + [local_agent.user_message(text, prompt, images)]
        self._ran, self._backed = [], set()
        worker = _Worker(self._number, project, config,
                         local_prompt.system_prompt(projects.get_mode(project)),
                         messages, text, prompt, images)
        queued = QtCore.Qt.QueuedConnection
        worker.event.connect(self._on_event, queued)
        worker.need_tool.connect(self._on_tool, queued)
        worker.need_leave.connect(self._on_leave, queued)
        worker.done.connect(self._on_done, queued)
        self._worker = worker
        self._live[:] = [worker]
        _threads.add(worker)
        worker.start()

    def stop(self):
        """Stop now. The worker ends on its own; whatever it still reports is dropped."""
        if not self.running():
            return
        worker, self._worker = self._worker, None
        self._live[:] = []
        self._number += 1
        worker.agent.stop()
        try:
            _save(self._project, self._safe + self._ran_messages() + [STOPPED])
        except OSError:
            pass
        self.finished.emit({"is_error": False, "result": "", "stopped": True})

    def _ran_messages(self):
        """The tools that ran after the last stored copy, as messages.

        The worker reports them with its next "state" only, and a stop drops
        that. Without them the model does not know of work that is in the
        document already, and builds it a second time.
        """
        if not self._ran:
            return []
        calls = [{"id": "stopped_%d_%d" % (len(self._safe), i), "type": "function",
                  "function": {"name": name, "arguments": json.dumps(args)}}
                 for i, (name, args, _) in enumerate(self._ran)]
        return ([{"role": "assistant", "content": "", "tool_calls": calls}]
                + [{"role": "tool", "tool_call_id": c["id"], "content": str(r[2])}
                   for c, r in zip(calls, self._ran)])

    # ---- from the worker, in the main thread ----------------------------------
    def _on_event(self, number, kind, data):
        if number != self._number:
            return
        if kind == "state":
            self._safe, self._ran = data, []
            try:
                _save(self._project, data)
            except OSError:
                pass
        elif kind in ("text", "answer"):
            self.text.emit(data)
        elif kind == "note":
            self.text.emit(f"*{data}*")
        elif kind == "tool_call":
            self.tool.emit(data["name"], _tool_detail(data["args"]))
        elif kind == "tool_result" and data["problem"]:
            self.tool_error.emit(data["name"])

    def _on_leave(self, number, call):
        """The worker is about to send a request: the gate of all outgoing requests decides."""
        try:
            call.result = number == self._number and bool(backends.may_contact(call.name, call.args))
        finally:
            call.done.set()

    def _on_tool(self, number, call):
        if number != self._number:
            call.done.set()               # a stopped run: its worker is leaving anyway
            return
        if self._in_tool:
            # a tool that opened a dialog lets events through; never run two tools at once
            QtCore.QTimer.singleShot(50, lambda: self._on_tool(number, call))
            return
        self._in_tool = True
        try:
            call.result = self._run_tool(call.name, call.args)
            if number == self._number:       # not after a stop from inside the tool
                self._ran.append((call.name, call.args, call.result))
        finally:
            self._in_tool = False
            call.done.set()

    def _run_tool(self, name, args):
        """Run one tool in the document of this run, whatever the user looks at now."""
        works_in_document = name != "read_project_file"
        if works_in_document and self._doc:
            if _document(self._doc) is None:
                # never go on in the document that happens to be active: it is other work
                return DOC_CLOSED % self._doc
            if self._doc != _active_name():
                # the user can click another document while the model thinks: go back to ours
                App.setActiveDocument(self._doc)
        if name in CHANGING:
            # the user decides, in this thread, before anything of the step happens
            if not approval.request(name, args, self._source):
                return approval.DECLINED
            self._changed = True
            self._backup()
        result = local_tools.run_tool(self._project, name, args)
        if works_in_document:
            self._doc = _active_name()       # the code can make the document, or go to another
        return result

    def _backup(self):
        """Before the first change of a request, keep the model file as it is on disk."""
        doc = _document(self._doc)
        path = doc.FileName if doc is not None else ""
        if not path or path in self._backed:
            return
        self._backed.add(path)
        try:
            copy = backup(self._project, path)
        except OSError as exc:
            self.text.emit(f"*No backup copy of the model could be made: {exc}*")
            return
        if copy:
            self.tool.emit("backup", "model/.backup/" + os.path.basename(copy))

    def _on_done(self, number, res):
        if number != self._number:
            return                        # the late result of a stopped run
        self._worker = None
        self._live[:] = []
        try:
            if not self._changed:
                pass                      # a question alone does not rewrite the user's file
            elif res.get("is_error") or res.get("incomplete"):
                # an error, the step limit, a cut answer or a problem that is still
                # there: the file on disk stays the last good state
                self.text.emit(NOT_SAVED)
            else:
                self._save_document()
        finally:
            self.finished.emit(res)       # whatever the save step does, the panel must hear the end

    def _save_document(self):
        """Save the model after a request that worked. Not left to the model."""
        doc = _document(self._doc)
        if doc is None:
            return
        try:
            if doc.FileName:
                doc.save()
                path = doc.FileName
            else:
                folder = os.path.join(self._project, "model")
                os.makedirs(folder, exist_ok=True)
                path, n = os.path.join(folder, doc.Name + ".FCStd"), 2
                while os.path.exists(path):          # never overwrite another model
                    path, n = os.path.join(folder, f"{doc.Name}-{n}.FCStd"), n + 1
                doc.saveAs(path)
        except Exception as exc:
            self.text.emit(f"*The document could not be saved: {exc}*")
            return
        name = os.path.basename(path)
        try:
            rel = os.path.relpath(os.path.realpath(path), os.path.realpath(self._project))
            if not rel.startswith(".."):
                name = rel.replace(os.sep, "/")
        except ValueError:
            pass                          # Windows: the document is on another drive than the project
        self.tool.emit("save", name)


def _active_name():
    doc = App.ActiveDocument
    return doc.Name if doc is not None else None


def _document(name):
    """The open document with this name, or None (the user can close it during a run)."""
    try:
        return App.getDocument(name) if name else None
    except Exception:
        return None


backup = projects.backup     # the bridge makes the same copies


def _open_model(project):
    """With no document open, go on with the project's newest model, as CLAUDE.md asks of Claude."""
    if App.ActiveDocument is not None:
        return
    try:
        model = projects.newest_model(project)
        if model:
            projects.open_model(model)
    except Exception as exc:
        App.Console.PrintWarning(f"CADprompt: could not open the project's model: {exc}\n")
