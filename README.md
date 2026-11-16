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
| **Claude account** | the Claude Code program, signed in, and the `uv` program | your messages, the project files that Claude reads, screenshots of FreeCAD: to Anthropic, on your subscription |
| **Anthropic API key** | the Claude Code program, `uv`, and a key | the same, to Anthropic, billed per use |

**Local model, step by step.** In Settings choose "Local model or other server",
type the server address (LM Studio: `http://127.0.0.1:1234/v1`, Ollama:
`http://127.0.0.1:11434/v1`), press **Find models**, pick one, OK.
For a classroom: run one model server on a computer in the room and let the
students type its address, for example `http://192.168.1.20:1234/v1`.

## Using it

- **Project**: a folder with `model/`, `refs/` and `drawings/`. Pick one or make a
  new one at the top of the panel.
- **Build / Teach**: Build makes the whole part. Teach goes one feature at a time
  and says which tool a person would use and why.
- **Add reference…**, or drop a photo, a PDF or a link on the panel: it goes into
  `refs/` for the AI.
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
- **Keys** are files that only you can read, in FreeCAD's user data folder
  (`cadprompt_api_key`, `cadprompt_local_key`). A key is sent only to the server
  it was saved for, and only over https unless the server is this computer.
- **Nothing is sent to an AI service until you send a message or press Find models.**
  With the Claude account source, the panel asks the Claude Code program on this
  computer whether you are signed in when it opens. No telemetry.
- With the Claude account and API key sources, the panel downloads the
  freecad-mcp bridge from GitHub the first time, after it asks you.

## What is not done yet

- The **API key** source still needs the Claude Code program. A version without
  it is planned.
- The **Claude account** source still needs `uv` and the freecad-mcp download.
  A bridge built into the addon is planned.
- The AI's code runs **without asking first**. A "show the code, then Run or
  Skip" step, with a switch for "run without asking", is planned. The official
  FreeCAD addon list requires it, so CADprompt is not in that list yet.
- **Pictures for the local model** (a photo of a sketch) are planned.
- No person has tested Windows and macOS.
- A fine-tuned small model for FreeCAD is planned.

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
