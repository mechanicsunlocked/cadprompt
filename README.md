<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/logo-dark.svg">
    <img src="docs/logo.svg" width="150" alt="CAD prompt">
  </picture>
</p>

<h1 align="center">CADprompt</h1>

<p align="center"><b>Say what to build. Watch FreeCAD build it.</b><br>
A chat panel in FreeCAD, for a local model, an API key or a signed-in AI account.
Linux, macOS and Windows.</p>

> **Status: test version 0.2.** It works on Linux with FreeCAD 1.1.4. Windows and
> macOS are written for but not yet tested by a person. Read "What is not done yet"
> before you give it to a class.

## Install

In FreeCAD, no command line:

1. **Edit → Preferences → Addon Manager Options**.
2. Under **Custom repositories** press **+**.
   Repository URL: `https://github.com/mechanicsunlocked/cadprompt`  Branch: `freecad-addon`
   (the test branch; it will be `main` after the tests on Windows and macOS)
3. Close the preferences, open **Tools → Addon manager**, search **CADprompt**, install.
4. Restart FreeCAD.

Updates come the same way: the Addon Manager shows them.

## First start

Pick the **CADprompt** workbench in FreeCAD's workbench list. The chat panel opens
on the right and stays open when you go to another workbench. Press **Settings**
in the panel and choose where the AI runs:

| Source | You need | What leaves the computer |
|---|---|---|
| **Local model** | a model server that speaks the OpenAI API: LM Studio, Ollama, llama.cpp. On this computer or on another one in your network. Nothing else. | nothing, when the server is on this computer; else to that server |
| **Anthropic API key** | a key from the Anthropic Console. Nothing else. | your messages, the images that you send with them, and what the tools report (the objects of your model, the output of the code, text files of the project that the AI reads): to `api.anthropic.com`, billed per use |
| **Claude account** | the Claude Code program, signed in. Nothing else. | your messages, the project files that Claude reads, what the tools report, and a picture of the 3D view when Claude asks for one: to Anthropic, on your subscription |

**Local model, step by step.** In Settings choose "Local model or other server",
type the server address (LM Studio: `http://127.0.0.1:1234/v1`, Ollama:
`http://127.0.0.1:11434/v1`), press **Find models**, pick one, OK.
For a classroom: run one model server on a computer in the room and let the
students type its address, for example `http://192.168.1.20:1234/v1`.

**API key, step by step.** In Settings choose "Anthropic API key", paste the
key, OK. The model is `claude-opus-5-5` until you pick another one: **Find
models** asks Anthropic which models your key can use.

**Claude account, step by step.** Install Claude Code, run `claude` one time in
a terminal and sign in. In Settings choose "Claude account". The panel starts
Claude Code for each message; you do not need the terminal again.

The local model and the API key share one conversation in a project. You can
change between them in the middle of the work.

## Using it

- **Project**: a folder with `model/`, `refs/` and `drawings/`. Pick one or make a
  new one at the top of the panel.
- **Build / Teach**: Build makes the whole part. Teach goes one feature at a time
  and says which tool a person would use and why.
- **Add reference…**, or drop a photo, a PDF or a link on the panel: it goes into
  `refs/` for the AI. With a local model or an API key, an image (PNG, JPEG) also
  goes with your next message, at most 4 for each message, made smaller to
  1568 pixels. Only the picture is sent, not the text that the file holds about
  its author, camera or place. The model must be able to read images; if it
  cannot, the panel tells you and you can send the message again without them.
- **Drawing**: a workshop drawing on an ISO sheet (A4 to A2): three views, an
  isometric view, the overall dimensions, the title block, and a PDF in
  `drawings/`. The sheet is made by fixed rules, not by the AI. Tolerances,
  surface marks and detail dimensions you add yourself.
- **Check**: the AI reviews the model before it goes to the shop.
- **Undo**: each step of the AI is one Undo step in FreeCAD. Before the first
  change of a request, the model file is copied to `model/.backup/`. A request
  that ends with an error is not saved over your file.

The **Drawing** toolbar of the workbench has the Hoischen tools for TechDraw
(DIN preset, dimension lines at 10 mm and 7 mm): see [docs/hoischen.md](docs/hoischen.md).

## Safety and privacy, the honest version

- **The AI writes Python and FreeCAD runs it, as you.** A guard refuses the plain
  ways to start programs, use the network or delete files, and one run is stopped
  after 60 seconds. That guard is a fence, not a wall. Work on models that you can
  restore.
- **A small local model makes mistakes.** It can build a wrong part and say that
  it is right. The panel finds broken objects and takes such a step back, but it
  cannot see a hole in the wrong place. Check the part.
- **Keys** are files in FreeCAD's user data folder (`cadprompt_api_key`,
  `cadprompt_local_key`), not in FreeCAD's settings. On Linux and macOS only you
  can read them (mode 0600). Windows has no such modes: there the files are in
  your own user folder, which other users of the computer cannot open. A key is
  sent only to the server
  it was saved for, and only over https unless the server is this computer.
- **Nothing is sent to an AI service until you send a message or press Find models.**
  With the Claude account source, the panel asks the Claude Code program on this
  computer whether you are signed in when it opens. No telemetry.
- With the API key source, each step of a request is one HTTPS request to
  `api.anthropic.com`: the conversation of the project (up to about 300,000
  characters of it), the images of that conversation and the key. Find models is
  one more request with the key. Nothing else is contacted.
- CADprompt connects to the AI service directly. It does not use a proxy from the
  settings of the computer, so a key never goes to a proxy. On a network that
  reaches the internet only through a proxy, the API key source does not work.
- With the Claude account source, Claude Code is a program of its own, and it
  must reach FreeCAD. For that, CADprompt opens a small server inside FreeCAD
  (the Model Context Protocol, over HTTP). The server listens only on this
  computer (`127.0.0.1`), on a port that the system picks. Each request needs a
  token that is made new for each start and is kept in memory only; the panel
  gives it to the Claude Code program that it starts. A web page cannot use the
  server: a request from a browser is refused. The server starts when you send
  a message to Claude, not when FreeCAD starts. It stops when you close the
  panel, choose a local model or an API key, or quit FreeCAD.
- Claude Code gets four tools from that server and may use them without a
  question of its own: `run_python`, `get_document_state`, `make_drawing` and
  `get_view`. It gets no shell: the project's settings deny that.

## What is not done yet

- The AI's code runs **without asking first**. A "show the code, then Run or
  Skip" step, with a switch for "run without asking", is planned (the place for
  it is `cadprompt/approval.py`; all sources go through it). The official
  FreeCAD addon list requires it, so CADprompt is not in that list yet.
- A question "may CADprompt contact this service?" before the first request to
  each AI service is planned (the place for it is `backends.may_contact`).
- No person has tested Windows and macOS.
- A fine-tuned small model for FreeCAD is planned.

## Projects from an older version

The first test versions needed the `uv` program and the FreeCADMCP addon for the
Claude account source. It needs neither now.

- When you pick a project that an older CADprompt made, the files that came
  from the template (`.mcp.json`, `CLAUDE.md`, two commands) are replaced by the
  new ones, and the old ones stay beside them as `<name>.old`. This happens only
  for a file that you never changed. A file that you changed is left alone, and
  the panel tells you what to do.
- CADprompt does not remove the FreeCADMCP addon. If you do not use it for other
  things, remove it in the Addon Manager: as long as it is installed, it opens
  its own port when FreeCAD starts.

## For developers

`python3 tests/run_all.py` runs all tests in a throw-away FreeCAD profile
(see [tests/README.md](tests/README.md)).

```
cadprompt/    the chat panel, the engines, the drawing tool
hoischen/     DIN helpers for TechDraw (LGPL-2.1-or-later)
template/     copied into every new project
tests/        run_all.py and the test files
```

The older Omarchy launcher (hotkeys, bar menu, `setup`) is at the tag
`omarchy-launcher`.

## Licence

MIT for CADprompt ([LICENSE](LICENSE)). The Hoischen tools in `hoischen/` are
LGPL-2.1-or-later ([hoischen/LICENSE](hoischen/LICENSE)).
