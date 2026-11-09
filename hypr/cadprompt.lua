-- cadprompt: five keys. Linked to ~/.config/hypr/cadprompt.lua
-- and loaded by one require line in hyprland.lua; ./setup remove takes both away.
o.bind("SUPER + ALT + C", "CAD prompt", "cadprompt")
o.bind("SUPER + ALT + SHIFT + C", "CAD prompt (teach mode)", "cadprompt --teach")
o.bind("SUPER + ALT + R", "Grab region into CAD project", "cadprompt-grab")
o.bind("SUPER + ALT + SHIFT + R", "Last download into CAD project", "cadprompt-grab last-download")
o.bind("SUPER + ALT + V", "Clipboard into CAD project", "cadprompt-grab clipboard")

-- No window rule: the prompt tiles like any other terminal (dwindle puts it
-- next to FreeCAD). Add one here if you ever want it floating again.
