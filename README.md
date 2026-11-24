<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/logo-dark.svg">
    <img src="docs/logo.svg" width="150" alt="CAD prompt">
  </picture>
</p>

<h1 align="center">CADprompt</h1>

<p align="center"><b>Say what to build. Watch FreeCAD build it.</b></p>

## Install

CADprompt is installed from inside FreeCAD. You don't need a terminal.

1. Open _Edit > Preferences > Addon Manager Options_.
2. Under _Custom repositories_, press _+_ and fill in:
   - Repository URL: `https://github.com/mechanicsunlocked/cadprompt`
   - Branch: `main`
3. Open _Tools > Addon manager_, search for _CADprompt_ and install it.
4. Restart FreeCAD.

Now pick the _CADprompt_ workbench. The chat panel opens on the right and asks
which AI you want to use. Choose one, and you're ready to build!

You need FreeCAD 1.0 or newer. It runs on Linux, macOS and Windows. Updates
come through the Addon Manager, like for any other addon.

## What it is

CADprompt is a chat panel inside FreeCAD. You write what you need, like "a
plate 100 x 60 x 8 with a 9 mm hole in each corner", and the AI builds it in
your open document. The sizes stay as parameters, so you can change one later
without starting over.

You choose the AI yourself:

- A local model on your own computer or in your own network (LM Studio,
  Ollama, llama.cpp). Nothing goes to the internet.
- An Anthropic API key. You pay per use.
- Your Claude account, through Claude Code.

And this is what you can do with it:

- _Build_ makes the whole part. _Teach_ goes one feature at a time and tells
  you which FreeCAD tool a person would use, and why. That one is for
  apprentices and students.
- _Drawing_ makes a workshop drawing on an ISO sheet: three views, an isometric
  view, the overall dimensions, the title block, and a PDF. The sheet follows
  fixed rules. The AI doesn't get to improvise there.
- _Welding_ puts fillet welds between your parts. The drawing then gets the
  ISO 2553 symbols, a weld list and a cut list.
- _Check_ lets the AI look over the model before it goes to the shop.

You stay in charge. Before any code runs, the panel shows it to you and waits
for _Run_ or _Skip_. Every step of the AI is one Undo step in FreeCAD. And
nothing is sent to any service before you have said _Allow_ to it.

## Before you rely on it

This is a test version (0.3). The automatic tests pass on Linux and on
Windows 11 with FreeCAD 1.1.4. macOS is written for, but no person has tried
it there yet.

A small local model makes mistakes. It can build a wrong part and tell you it
is right. So check the part before you cut metal!

## More

- [Manual](docs/manual.md): how to connect each AI, and everything the panel
  can do.
- [Safety and privacy](docs/privacy.md): what is sent where, where your keys
  are kept, and what is not done yet. Read it before you give CADprompt to a
  class.
- [Hoischen tools](docs/hoischen.md): DIN dimensioning for TechDraw. They come
  with the addon.

## For developers

`python3 tests/run_all.py` runs all tests in a throw-away FreeCAD profile
(see [tests/README.md](tests/README.md)).

```
cadprompt/    the chat panel, the engines, the drawing tool
hoischen/     DIN helpers for TechDraw (LGPL-2.1-or-later)
weldment/     the Weldment workbench, a pinned copy (LGPL-2.1-or-later): do not edit here,
              change freecad-weldment and run tools/vendor_weldment.py
tools/        vendor_weldment.py
template/     copied into every new project
tests/        run_all.py and the test files
```

The older Omarchy launcher (hotkeys, bar menu, `setup`) is at the tag
`omarchy-launcher`.

## Licence

MIT for CADprompt ([LICENSE](LICENSE)). The Hoischen tools in `hoischen/` are
LGPL-2.1-or-later ([hoischen/LICENSE](hoischen/LICENSE)), and so is the
Weldment workbench in `weldment/` ([weldment/LICENSE](weldment/LICENSE)).
