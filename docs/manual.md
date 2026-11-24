# CADprompt manual

How to connect an AI, and what the panel can do. For what is sent where, read
[Safety and privacy](privacy.md).

## First start

Pick the **CADprompt** workbench in FreeCAD's workbench list. The chat panel opens
on the right and stays open when you go to another workbench. A new installation
has no AI: the panel shows three buttons, **Local model**, **API key** and
**Claude account**. Nothing is contacted, and no login or key of your computer is
used, before you choose one. You can change the choice later in **Settings**:

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

**How fast the Claude account is.** Almost all of the waiting time is Claude
thinking and writing, not FreeCAD. Settings has **Thinking** for it: short,
medium (the standard) or long. Measured with Opus 5.5: a plate with four holes
takes about 20 s with short, 25 s with medium, and 45 s when Claude Code
decides by itself. For a difficult part, short can cost more time than it
saves, because a step fails and Claude does it again. In build mode Claude
puts the steps that belong together into one piece of code, so a simple part
is one **Run**. **Run all for this request** saves you the other clicks.

The local model and the API key share one conversation in a project. You can
change between them in the middle of the work.

**CADprompt asks before data leaves your computer.** The first time you use a
service that is not on this computer (a model server on another computer, the
Anthropic API, Claude Code), a window in FreeCAD names the service and says
what is sent. **Allow** is kept for that service; **Not now** sends nothing.
A model server on this computer needs no question. **Settings → Privacy**
lists the services that you allowed, with a **Forget** button for each. Forget
works at once: Claude Code is stopped when it is at work for that service.

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
- **Welding**: say "weld the upright to the plate, a4" or type `/weld`. The
  AI lists the corners where two parts meet (`find_joints`) and makes a
  parametric fillet weld in each one you agree to (`make_weld`): throat `a`,
  whole seam or intermittent `n × l (e)`, one or both sides. You approve each
  weld with one line ("Fillet weld a4, whole seam between Plate.Face6 and
  Upright.Face1"). The welds come from the
  [Weldment](https://github.com/mechanicsunlocked/freecad-weldment) workbench,
  which is part of this addon: its buttons are on the **Weld** toolbar, for
  welding by hand. The **Drawing** of a welded part shows all its parts and
  beads, an ISO 2553 symbol for each weld, and a second page with the weld
  list and the cut list (a PDF of its own).
- **Run or Skip**: when the AI wants to run code, the panel shows the code and
  waits. When the code is longer or wider than its box, a line under it says so:
  scroll to read all of it. **Run** runs exactly that text. **Skip** runs nothing
  and tells the AI that you did not approve the step. The question never takes
  the keyboard: what you type goes into the message box. After a click on the
  code, Enter is Run and Esc is Skip. **Run all for this
  request** runs the next steps of this one request without a question. A
  drawing asks the same way, with one line that says what will be made. Tools
  that only look (the list of objects, a file of the project) do not ask. The
  chat keeps every piece of code that ran, folded: press "show" to read it.
- **Run the AI's code without asking** (Settings): off in a new installation.
  When you turn it on, a warning tells you what that means. While it is on,
  the panel shows the marker "runs without asking"; one click on the marker
  turns it off again.
- **Training data** (Settings): off in a new installation. When you turn it
  on, the good requests to a local model or with an API key are also kept in
  the file `training/requests.jsonl` of the project. Nothing is sent. See
  [Training data](privacy.md#training-data-a-file-in-the-project-off-until-you-turn-it-on).
- **Undo**: each piece of code that ran is one Undo step in FreeCAD. Before the first
  change of a request, the model file is copied to `model/.backup/`. A request
  that ends with an error is not saved over your file.

The **Drawing** toolbar of the workbench has the Hoischen tools for TechDraw
(DIN preset, dimension lines at 10 mm and 7 mm): see [hoischen.md](hoischen.md).

## Projects from an older version

The first test versions needed the `uv` program and the FreeCADMCP addon for the
Claude account source. It needs neither now.

- When you send the first message to Claude in a project that an older
  CADprompt made, the files that came from the template (`.mcp.json`,
  `CLAUDE.md`, the commands, the settings of Claude Code) are replaced by the
  new ones, and the old ones stay beside them as `<name>.old`. The panel names
  the files. This happens only for a file that you never changed. A file that
  you changed is left alone, and the panel tells you what to do. With a local
  model or an API key nothing is replaced.
- The older terminal launcher works with the old files. To use it again for
  such a project, put the `.old` files back.
- CADprompt does not remove the FreeCADMCP addon. If you do not use it for other
  things, remove it in the Addon Manager: as long as it is installed, it opens
  its own port when FreeCAD starts.
