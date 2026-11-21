"""Building blocks for the code of a small model: `cad` in run_python.

A small model fails on the raw FreeCAD API: the text of an expression, the
arguments of a Placement, tool bodies that stay in the document, edges by
number. Each function here does one workshop job in one short line. It binds
the sizes to the VarSet "Params", returns the object that is now the part and
prints one line of what it made.

Only standard FreeCAD objects are made (Part primitives, cuts and fuses,
Part::Fillet, App::VarSet, expressions). The file opens and can be changed in
a FreeCAD that does not have this addon.

Places: millimetres and degrees, in the coordinates of the document.
cad.plate lies like a Part::Box (x from 0 to length, y from 0 to width, z from
0 to thickness), so x and y of a hole are its distances from two edges. A
shaft, a tube and a pipe stand on the origin along z; axis="x" or "y" lays
them down. Holes and slots go through along z; a depth makes them blind from
the top, an axis turns a hole. A hole, a slot or a pattern without a place is
in the middle of the part. A hole or a slot that would break through a side
of the part (an open notch) is refused.

A part can be given as the object, its Name or its Label. When a later step
used it up (the plate, after its holes were cut), the newest result of that
part is taken: no second body is made from the same base.

A wrong call raises ValueError or TypeError with one correct example, and
leaves nothing in the document.
"""

import functools
import inspect
import math

import FreeCAD as App
import Part

__all__ = ["plate", "shaft", "tube", "pipe", "frame", "hole", "hole_grid", "bolt_circle", "slot",
           "fillet", "chamfer", "move", "join"]

PARAMS = "Params"         # the one VarSet that holds the sizes
EXAMPLES = {}             # name of the function -> one correct call (filled by _helper)
MAX_HOLES = 200           # in one pattern: more is a mistake in the numbers
TOL = 1e-6

ATTACHED = ("AttachmentSupport", "Support", "_Body")     # links that do not use an object up
COPIES = ("Part::Mirroring",)                             # their source stays a body of its own
SIDES = {"x": ("XMin", "XMax"), "y": ("YMin", "YMax"), "z": ("ZMin", "ZMax")}
# Turns a shape that is built along z so that it runs along x or y. Along x the two sizes of
# a section lie along y and z, along y they lie along x and z.
TURN = {"z": App.Rotation(), "x": App.Rotation(App.Vector(1, 1, 1), 120), "y": App.Rotation(App.Vector(1, 0, 0), -90)}
LETTERS = {"ä": "ae", "ö": "oe", "ü": "ue", "Ä": "Ae", "Ö": "Oe", "Ü": "Ue", "ß": "ss"}
EDGE_WORDS = {"vertical": "z", "corners": "z", "upright": "z"}
# Other words for an argument, taken where the function has no argument of that name.
SPELLINGS = (("d", "diameter"), ("dia", "diameter"), ("outer_diameter", "diameter"), ("outside_diameter", "diameter"),
             ("inner_diameter", "bore"), ("inside_diameter", "bore"), ("t", "thickness"), ("h", "thickness"),
             ("height", "thickness"), ("t", "wall"), ("thickness", "wall"), ("wall_thickness", "wall"),
             ("l", "length"), ("height", "length"), ("h", "length"), ("w", "width"), ("r", "radius"),
             ("distance", "size"), ("centre", "center"),
             ("n", "count"), ("holes", "count"), ("number", "count"), ("columns", "nx"), ("cols", "nx"),
             ("rows", "ny"), ("pitch_x", "dx"), ("pitch_y", "dy"), ("pitch", "dx"), ("spacing", "dx"),
             ("margin", "edge"), ("pcd", "circle"), ("circle_diameter", "circle"), ("start_angle", "start"),
             ("at", "x"), ("position", "x"), ("positions", "x"), ("points", "x"), ("center", "x"),
             ("centre", "x"), ("obj", "part"), ("label", "name"))


class _Wrong(Exception):
    """A call that cannot be done. _helper adds the example and raises ValueError or TypeError."""

    def __init__(self, text, kind=ValueError):
        Exception.__init__(self, text)
        self.kind = kind


def _helper(example):
    """Marks a public function: other spellings of its arguments, and errors that show `example`."""
    def wrap(func):
        names = inspect.signature(func).parameters

        @functools.wraps(func)
        def call(*args, **kwargs):
            for other, name in SPELLINGS:
                if other in kwargs and other not in names and name in names and name not in kwargs:
                    kwargs[name] = kwargs.pop(other)
            try:
                inspect.signature(func).bind(*args, **kwargs)
            except TypeError as exc:
                raise TypeError("cad.%s: %s.\nExample: %s" % (func.__name__, exc, example)) from None
            try:
                return func(*args, **kwargs)
            except _Wrong as exc:
                raise exc.kind("cad.%s: %s\nExample: %s" % (func.__name__, exc, example)) from None
        call.example = EXAMPLES[func.__name__] = example
        return call
    return wrap


# ---- arguments -----------------------------------------------------------------------
def _text(value):
    """A number as short text: 12, 10.5."""
    return "%g" % (round(value, 4) + 0.0)        # + 0.0: never "-0"


def _number(value, what):
    """A number as a float. Also takes a FreeCAD quantity and text such as "12" or "12 mm"."""
    number = None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
    elif hasattr(value, "Value"):
        number = float(value.Value)
    elif isinstance(value, str):
        try:
            number = float(App.Units.Quantity(value).Value)
        except Exception:
            pass
    if number is None or math.isnan(number) or math.isinf(number):
        raise _Wrong("the %s must be a number, not %r." % (what, value), TypeError)
    return number


def _size(value, what):
    number = _number(value, what)
    if number <= 0:
        raise _Wrong("the %s must be above 0, not %s." % (what, _text(number)))
    return number


def _count(value, what):
    number = _number(value, what)
    if number != int(number) or number < 1:
        raise _Wrong("the %s must be a whole number from 1, not %s." % (what, _text(number)))
    return int(number)


def _axis(value):
    axis = str(value).strip().lower().lstrip("+")
    if axis not in SIDES:
        raise _Wrong('the axis must be "x", "y" or "z", not %r.' % (value,))
    return axis


def _is_point(value):
    return isinstance(value, App.Vector) or (isinstance(value, (list, tuple)) and len(value) in (2, 3)
                                             and all(isinstance(v, (int, float)) for v in value))


def _across(point, axis, what="place"):
    """The two numbers of a point that lie across `axis`. A point is (u, v), (x, y, z) or a Vector."""
    if not _is_point(point):
        raise _Wrong("a %s is two numbers such as (20, 30), not %r." % (what, point), TypeError)
    numbers = [float(v) for v in point]
    if len(numbers) == 3:
        numbers = [numbers[i] for i, a in enumerate("xyz") if a != axis]
    return tuple(numbers)


def _flat(values):
    for value in values:
        if isinstance(value, (list, tuple)):
            yield from _flat(value)
        else:
            yield value


def _identifier(name):
    """A name that FreeCAD takes for an object and that an expression can read."""
    text = "".join(LETTERS.get(c, c) for c in str(name).strip())
    text = "".join(c if c.isascii() and (c.isalnum() or c == "_") else "_" for c in text).strip("_")
    if not text or not text[0].isalpha():
        text = "Part_" + text
    text = text[0].upper() + text[1:]
    if text.isalpha():
        # "T", "Ohm": an expression would read a unit, not the object. The unit parser gets the
        # name alone and only letters, never a number before it: it reads "1 Welle" as 1 W + e l,
        # and that error is not a Python error, it ends FreeCAD.
        try:
            App.Units.Quantity(text)
            text += "_part"
        except Exception:
            pass
    return text


# ---- the part behind an argument -------------------------------------------------------
def _solids(obj):
    shape = getattr(obj, "Shape", None)
    try:
        return list(shape.Solids) if shape is not None and not shape.isNull() else []
    except Exception:
        return []


def _linked(value):
    """The document objects in the value of a link property, whatever its kind."""
    if isinstance(value, (list, tuple)):
        for item in value:
            yield from _linked(item)
    elif isinstance(value, App.DocumentObject):
        yield value


def users(obj):
    """The solid objects that are made from `obj`: through a link, not through an expression.
    The report of run_python uses it too."""
    out = []
    for other in obj.InList:
        if other.TypeId in COPIES or not other.isDerivedFrom("Part::Feature"):
            continue
        for name in other.PropertiesList:
            if name in ATTACHED or "Link" not in other.getTypeIdOfProperty(name):
                continue
            try:
                if obj in _linked(other.getPropertyByName(name)):
                    out.append(other)
                    break
            except Exception:
                pass
    return out


def newest(obj):
    """The last result that was made from `obj`, or `obj` itself. make_drawing uses it too."""
    seen = {obj.Name}
    while True:
        later = [u for u in users(obj) if u.Name not in seen]
        if not later:
            return obj
        obj = max(later, key=lambda u: u.ID)
        seen.add(obj.Name)


def _parts(doc):
    """The names of the parts of the document: every solid object that nothing was made from."""
    if doc is None:
        return []
    return [o.Name for o in doc.Objects if _solids(o) and not users(o) and getattr(o, "Visibility", True)]


def _find(part):
    """(the newest result of the part that `part` names, the name that was given)."""
    if isinstance(part, str):
        doc = App.ActiveDocument
        objects = doc.Objects if doc is not None else []
        obj = doc.getObject(part) if doc is not None else None
        if obj is None:
            wanted = part.strip().lower()
            obj = next((o for o in objects if wanted in (o.Label.lower(), o.Name.lower())), None)
        if obj is None:
            raise _Wrong('there is no object "%s" in the document. Its parts are: %s.'
                         % (part, ", ".join(_parts(doc)) or "none"))
    elif isinstance(part, App.DocumentObject):
        obj = part
        try:
            obj.Name
        except Exception:
            raise _Wrong("that object is not in the document any more (its run was taken back, or it "
                         "was deleted). Make the part again.") from None
    else:
        raise _Wrong("the first argument must be the part (the object, its name or its label), not %r. "
                     "The parts of the document are: %s." % (part, ", ".join(_parts(App.ActiveDocument)) or "none"),
                     TypeError)
    given = obj.Name
    obj = newest(obj)
    if not _solids(obj):
        raise _Wrong("%s is not a solid part. The parts of the document are: %s."
                     % (obj.Name, ", ".join(_parts(obj.Document)) or "none"))
    return obj, given


def _of(obj, given):
    """How the printed line names the part that was worked on."""
    if obj.Name == given:
        return obj.Name
    return "%s (the newest result of %s)" % (obj.Name, given)


# ---- where a part is --------------------------------------------------------------------
def _true_box(shape):
    """The bounding box of the shape itself. Shape.BoundBox is too large for some curved faces."""
    try:
        return shape.optimalBoundingBox(False, False)
    except Exception:
        return shape.BoundBox


class _Where:
    """The sides of a part, as numbers and as the text of an expression.

    The expression reads the bounding box of the part, so a hole stays in the
    middle and stays a through hole when a size of the part changes. Where
    FreeCAD does not give the right number for that expression, the number of
    today is used and `loose` is set: the bounding box that an expression can
    read is too large for a part with a rounded edge, and an object with the
    name of a unit ("T", "Pa") cannot be read at all.
    """

    def __init__(self, base):
        self.base, self.loose, self.texts = base, False, {}
        self.box = _true_box(base.Shape)

    def number(self, side):
        return getattr(self.box, side)

    def side(self, side):
        if side not in self.texts:
            text = "%s.Shape.BoundBox.%s * 1 mm" % (self.base.Name, side)
            try:
                follows = abs(self.base.evalExpression(text).Value - self.number(side)) < TOL
            except Exception:
                follows = False
            self.texts[side] = text if follows else "%.12g mm" % self.number(side)
        self.loose = self.loose or "BoundBox" not in self.texts[side]
        return self.texts[side]

    def middle(self, axis):
        low, high = SIDES[axis]
        return "(%s + %s) / 2" % (self.side(low), self.side(high))

    def middle_number(self, axis):
        low, high = SIDES[axis]
        return (self.number(low) + self.number(high)) / 2

    def inside(self, axis, value, reach=None):
        """Is the place on the part, or with `reach`: is a cut that goes that far to both sides
        of the place fully in the part, with a wall left?"""
        low, high = SIDES[axis]
        if reach is None:
            return self.number(low) - TOL <= value <= self.number(high) + TOL
        return self.number(low) + TOL < value - reach and value + reach < self.number(high) - TOL

    def span(self, axis):
        low, high = SIDES[axis]
        return "%s to %s" % (_text(self.number(low)), _text(self.number(high)))


# ---- one call: what it adds, and how it is taken back ------------------------------------
def _problem(obj):
    """Why FreeCAD could not build this object, or ''."""
    try:
        if "Invalid" in obj.State:
            return " ".join(obj.getStatusString().split())
        shape = getattr(obj, "Shape", None)
        if shape is None:
            return ""
        if shape.isNull() or not shape.BoundBox.isValid():
            return "the result is empty"
        return "" if shape.isValid() else "the shape is not valid"
    except Exception as exc:
        return str(exc)


class _Build:
    """The objects and parameters of one helper call. All of it goes away when the call fails."""

    def __init__(self, doc=None):
        self.opened = doc is None and App.ActiveDocument is None
        self.doc = doc or App.ActiveDocument or App.newDocument("Model")
        self.before = {o.Name for o in self.doc.Objects}
        self.params = None
        self.bound = []          # the parameters of this call, for the printed line
        self.added = []          # those that were not there before
        self.back = []           # calls that put changed things back

    def __enter__(self):
        return self

    def __exit__(self, kind, exc, tb):
        if kind is not None:
            self.undo()
        return False

    def undo(self):
        doc = self.doc
        if self.opened:
            App.closeDocument(doc.Name)
            return
        for name in reversed([o.Name for o in doc.Objects if o.Name not in self.before]):
            try:
                doc.removeObject(name)
            except Exception:
                pass
        if self.params is not None and self.params.Name in self.before:
            for name in self.added:
                try:
                    self.params.removeProperty(name)
                except Exception:
                    pass
        for call in reversed(self.back):
            try:
                call()
            except Exception:
                pass
        doc.recompute()

    def _varset(self, make=True):
        if self.params is None:
            # by name, by label, then the one that an earlier call made when the name was taken
            sets = [o for o in self.doc.Objects if o.TypeId == "App::VarSet"]
            found = ([o for o in sets if o.Name == PARAMS] or [o for o in sets if o.Label == PARAMS]
                     or [o for o in sets if o.Name.startswith(PARAMS)] or [None])[0]
            if found is None and make:
                found = self.doc.addObject("App::VarSet", PARAMS)
            self.params = found
        return self.params

    def tag(self, name, default):
        """A name that no object and no parameter has yet: of the object and of its parameters."""
        base = _identifier(name) if name else default
        params = self._varset(make=False)
        used = list(params.PropertiesList) if params is not None else []
        tag, n = base, 2
        while self.doc.getObject(tag) is not None or any(p.startswith(tag + "_") for p in used):
            tag, n = "%s%d" % (base, n), n + 1
        return tag

    def param(self, tag, what, value, kind="Length"):
        """Put <tag>_<what> into Params and return the text of the expression that reads it."""
        params = self._varset()
        name = "%s_%s" % (tag, what)
        if name in params.PropertiesList:
            self.back.append(functools.partial(setattr, params, name, getattr(params, name).Value))
        else:
            params.addProperty("App::Property" + kind, name, tag, "")
            self.added.append(name)
        setattr(params, name, value)
        self.bound.append(name)
        return "%s.%s" % (params.Name, name)

    def add(self, kind, name, label=None):
        obj = self.doc.addObject(kind, name)
        if label:
            obj.Label = str(label)
        return obj

    def bind(self, obj, prop, expression):
        obj.setExpression(prop, expression)

    def hide(self, *objects):
        """Switch the inputs of a result off: FreeCAD's window does not do it for every kind."""
        for obj in objects:
            try:
                if obj.Visibility:
                    obj.Visibility = False
                    self.back.append(functools.partial(setattr, obj, "Visibility", True))
            except Exception:
                view = getattr(obj, "ViewObject", None)      # a FreeCAD without that property
                if view is not None:
                    view.Visibility = False

    def name_after(self, result, base):
        """The result is the part now, so it takes the label of the part: the tree and the
        drawing keep the name "Plate" when the last step was a fillet. Names do not change."""
        label = base.Label
        self.back.append(functools.partial(setattr, base, "Label", label))
        base.Label = base.Name if base.Name != label else label + "_blank"
        result.Label = label

    def done(self, result):
        """Recompute, and make sure that FreeCAD built everything of this call."""
        self.doc.recompute()
        for obj in self.doc.Objects:
            if obj.Name not in self.before or obj is result:
                problem = _problem(obj)
                if problem:
                    raise _Wrong("FreeCAD could not build %s: %s." % (obj.Name, problem))
        if not _solids(result):
            raise _Wrong("the result has no solid.")
        return result

    def cut(self, base, tools, tag, what="hole"):
        """`base` less the tools, as one Part::Cut. The inputs are used up and switched off."""
        volume, pieces = base.Shape.Volume, len(_solids(base))
        tool = tools[0]
        if len(tools) > 1:
            tool = self.add("Part::MultiFuse", tag + "_Tools")
            tool.Shapes = tools
        result = self.add("Part::Cut", tag)
        result.Base, result.Tool = base, tool
        self.name_after(result, base)
        self.hide(base, tool, *tools)
        self.doc.recompute()
        if "Invalid" not in result.State:
            if not _solids(result):
                raise _Wrong("the %s takes the whole part away." % what)
            if len(_solids(result)) != pieces:
                raise _Wrong("the %s cuts the part into %d pieces." % (what, len(_solids(result))))
            if result.Shape.Volume > volume - TOL:
                raise _Wrong("the %s takes nothing away: the part has no material at that place." % what)
        return self.done(result)

    def line(self, result, what, loose=()):
        """Print the one line that goes back to the model."""
        names = self.bound
        if len(names) > 8:
            names = names[:6] + ["... (%d in all)" % len(names)]
        text = "%s: %s." % (result.Name, what)
        text += " Params: %s." % ", ".join(names) if names else ""
        text += " Not bound: %s." % ", ".join(loose) if loose else ""
        # a first try or a tool body that was left behind: the user sees it beside the part
        others = [n for n in _parts(self.doc) if n != result.Name]
        if others:
            text += " Other parts in the document: %s." % ", ".join(others[:5] + ["..."] * (len(others) > 5))
        print(text)
        return result


# ---- blanks -------------------------------------------------------------------------------
@_helper("cad.plate(120, 80, 10)")
def plate(length, width, thickness, name=None):
    """A plate, a flat bar or a block: x from 0 to length, y from 0 to width, z from 0 to thickness.

        plate = cad.plate(120, 80, 10)
    """
    sizes = [_size(v, w) for v, w in ((length, "length"), (width, "width"), (thickness, "thickness"))]
    with _Build() as b:
        tag = b.tag(name, "Plate")
        box = b.add("Part::Box", tag, name)
        for prop, what, value in zip(("Length", "Width", "Height"), ("Length", "Width", "Thickness"), sizes):
            b.bind(box, prop, b.param(tag, what, value))
        b.done(box)
        return b.line(box, "plate %s x %s x %s" % tuple(map(_text, sizes)))


@_helper("cad.shaft(30, 40, 20, 60)")
def shaft(*steps, axis="z", name=None, diameter=None, length=None):
    """A round bar, a disc or a stepped shaft from the origin along z: diameter, length of each step.
    axis="x" lays it along x.

        shaft = cad.shaft(30, 40, 20, 60)      # d30 for 40, then d20 for 60
        disc = cad.shaft(120, 12)
    """
    numbers = list(_flat(steps)) + [v for v in (diameter, length) if v is not None]
    if numbers and isinstance(numbers[-1], str) and numbers[-1].strip().lower() in SIDES:
        axis = numbers.pop()                 # cad.shaft(30, 40, "x")
    if not numbers or len(numbers) % 2:
        raise _Wrong("give a diameter and a length for each step, %d numbers were given." % len(numbers))
    numbers = [_size(v, "length" if i % 2 else "diameter") for i, v in enumerate(numbers)]
    pairs = list(zip(numbers[::2], numbers[1::2]))
    axis = _axis(axis)
    with _Build() as b:
        tag = b.tag(name, "Shaft")
        if len(pairs) == 1:
            result = b.add("Part::Cylinder", tag, name)
            b.bind(result, "Radius", b.param(tag, "Diameter", pairs[0][0]) + " / 2")
            b.bind(result, "Height", b.param(tag, "Length", pairs[0][1]))
        else:
            parts, lengths = [], []
            for n, (thick, long) in enumerate(pairs, 1):
                step = b.add("Part::Cylinder", "%s_Step%d" % (tag, n))
                b.bind(step, "Radius", b.param(tag, "D%d" % n, thick) + " / 2")
                if lengths:
                    b.bind(step, "Placement.Base.z", " + ".join(lengths))      # on top of the steps before it
                lengths.append(b.param(tag, "L%d" % n, long))
                b.bind(step, "Height", lengths[-1])
                parts.append(step)
            result = b.add("Part::MultiFuse", tag, name)
            result.Shapes, result.Refine = parts, True
            b.hide(*parts)
        result.Placement = App.Placement(App.Vector(), TURN[axis])
        b.done(result)
        if len(_solids(result)) != 1:
            raise _Wrong("the steps do not make one solid.")
        what = " + ".join("d%s x %s" % (_text(d), _text(long)) for d, long in pairs)
        return b.line(result, "shaft along %s, %s, %s long" % (axis, what, _text(sum(p[1] for p in pairs))))


@_helper("cad.tube(60, 40, 4, 200)")
def tube(width, height, wall, length, axis="z", name=None):
    """A square or rectangular tube with sharp corners. It stands on the origin along z, the axis
    in the middle of the section: width along x, height along y.

        tube = cad.tube(60, 40, 4, 200)        # 60 x 40 x 4, 200 long
    """
    width, height, wall, length = (_size(v, w) for v, w in (
        (width, "width"), (height, "height"), (wall, "wall"), (length, "length")))
    if 2 * wall >= min(width, height):
        raise _Wrong("the wall %s is too thick for a section of %s x %s."
                     % (_text(wall), _text(width), _text(height)))
    axis = _axis(axis)
    with _Build() as b:
        tag = b.tag(name, "Tube")
        w, h, t, long = (b.param(tag, what, v) for what, v in (
            ("Width", width), ("Height", height), ("Wall", wall), ("Length", length)))
        outside = b.add("Part::Box", tag + "_Outside")
        inside = b.add("Part::Box", tag + "_Inside")
        # longer than the tube at both ends: no face of the hollow lies on a face of the tube
        inside.Placement = App.Placement(App.Vector(0, 0, -1), App.Rotation())
        for obj, less, end in ((outside, "", ""), (inside, " - 2 * " + t, " + 2 mm")):
            b.bind(obj, "Length", w + less)
            b.bind(obj, "Width", h + less)
            b.bind(obj, "Height", long + end)
            b.bind(obj, "Placement.Base.x", "-(%s) / 2" % (w + less))
            b.bind(obj, "Placement.Base.y", "-(%s) / 2" % (h + less))
        result = b.add("Part::Cut", tag, name)
        result.Base, result.Tool = outside, inside
        result.Placement = App.Placement(App.Vector(), TURN[axis])
        b.hide(outside, inside)
        b.done(result)
        return b.line(result, "tube %s x %s x %s along %s, %s long"
                      % (_text(width), _text(height), _text(wall), axis, _text(length)))


@_helper("cad.pipe(48.3, 3.2, 250)")
def pipe(diameter, wall=None, length=None, bore=None, axis="z", name=None):
    """A round tube, a sleeve or a ring from the origin along z: outside diameter, wall, length.
    Give bore= in place of the wall when the inside diameter is known.

        pipe = cad.pipe(48.3, 3.2, 250)
        sleeve = cad.pipe(20, bore=10.5, length=15)
    """
    diameter = _size(diameter, "diameter")
    if length is None:
        raise _Wrong("the length is missing.")
    length = _size(length, "length")
    if (wall is None) == (bore is None):
        raise _Wrong("give the wall or bore=, one of the two.")
    inside = _size(bore, "bore") if wall is None else diameter - 2 * _size(wall, "wall")
    if not 0 < inside < diameter:
        raise _Wrong("the %s does not fit into the diameter %s." % (
            "bore %s" % _text(bore) if wall is None
            else "wall %s (for an inside diameter give bore=)" % _text(wall), _text(diameter)))
    axis = _axis(axis)
    with _Build() as b:
        tag = b.tag(name, "Pipe")
        d = b.param(tag, "Diameter", diameter)
        hollow = (b.param(tag, "Bore", inside) + " / 2" if wall is None
                  else "%s / 2 - %s" % (d, b.param(tag, "Wall", wall)))
        long = b.param(tag, "Length", length)
        outside = b.add("Part::Cylinder", tag + "_Outside")
        inner = b.add("Part::Cylinder", tag + "_Inside")
        b.bind(outside, "Radius", d + " / 2")
        b.bind(outside, "Height", long)
        b.bind(inner, "Radius", hollow)
        b.bind(inner, "Height", long + " + 2 mm")
        inner.Placement = App.Placement(App.Vector(0, 0, -1), App.Rotation())
        result = b.add("Part::Cut", tag, name)
        result.Base, result.Tool = outside, inner
        result.Placement = App.Placement(App.Vector(), TURN[axis])
        b.hide(outside, inner)
        b.done(result)
        return b.line(result, "pipe d%s, bore d%s, along %s, %s long"
                      % (_text(diameter), _text(inside), axis, _text(length)))


@_helper("cad.frame(500, 300, 30, 30, 2)")
def frame(length, width, profile_width, profile_height, wall, name=None):
    """A flat frame of four bars from a square or rectangular tube, the corners mitred at 45
    degrees. Outside size length x width, like a plate: x from 0 to length, y from 0 to width.
    The profile is profile_width wide (in the frame) and profile_height high (along z). The
    result is one object that holds the four bars as four solids.

        frame = cad.frame(500, 300, 30, 30, 2)     # 500 x 300 from tube 30 x 30 x 2
    """
    length, width, a, high, wall = (_size(v, w) for v, w in (
        (length, "length"), (width, "width"), (profile_width, "profile width"),
        (profile_height, "profile height"), (wall, "wall")))
    if 2 * wall >= min(a, high):
        raise _Wrong("the wall %s is too thick for a profile of %s x %s." % (_text(wall), _text(a), _text(high)))
    if 2 * a >= min(length, width):
        raise _Wrong("a profile %s wide does not fit into a frame of %s x %s."
                     % (_text(a), _text(length), _text(width)))
    with _Build() as b:
        tag = b.tag(name, "Frame")
        along, across, pa, ph, t = (b.param(tag, what, v) for what, v in (
            ("Length", length), ("Width", width), ("ProfileWidth", a), ("ProfileHeight", high), ("Wall", wall)))
        # Each bar is built along x with its outside face at y = 0, then turned into its place.
        places = ((along, 0, None, None), (across, 90, along, None), (along, 180, along, across),
                  (across, -90, None, across))
        bars = []
        for n, (long, angle, x, y) in enumerate(places, 1):
            outside = b.add("Part::Wedge", "%s_Bar%d_Outside" % (tag, n))
            inside = b.add("Part::Wedge", "%s_Bar%d_Inside" % (tag, n))
            for prop in ("Xmin", "Ymin", "Zmin", "Z2min"):
                setattr(outside, prop, 0)
            for prop, text in (("Xmax", long), ("Ymax", pa), ("Zmax", ph), ("Z2max", ph), ("X2min", pa),
                               ("X2max", "%s - %s" % (long, pa))):
                b.bind(outside, prop, text)
            # the hollow goes 1 mm out through both mitre faces, so no face lies on a face
            for prop, text in (("Ymin", t), ("Zmin", t), ("Z2min", t), ("Ymax", "%s - %s" % (pa, t)),
                               ("Zmax", "%s - %s" % (ph, t)), ("Z2max", "%s - %s" % (ph, t)),
                               ("Xmin", "%s - 1 mm" % t), ("Xmax", "%s - %s + 1 mm" % (long, t)),
                               ("X2min", "%s - %s - 1 mm" % (pa, t)),
                               ("X2max", "%s - %s + %s + 1 mm" % (long, pa, t))):
                b.bind(inside, prop, text)
            bar = b.add("Part::Cut", "%s_Bar%d" % (tag, n))
            bar.Base, bar.Tool = outside, inside
            bar.Placement = App.Placement(App.Vector(), App.Rotation(App.Vector(0, 0, 1), angle))
            for letter, text in (("x", x), ("y", y)):
                if text:
                    b.bind(bar, "Placement.Base." + letter, text)
            b.hide(outside, inside)
            bars.append(bar)
        result = b.add("Part::Compound", tag, name)
        result.Links = bars
        b.hide(*bars)
        b.done(result)
        if len(_solids(result)) != 4:
            raise _Wrong("the four bars could not be built.")
        return b.line(result, "frame %s x %s of 4 mitred bars (4 solids) from tube %s x %s x %s"
                      % tuple(map(_text, (length, width, a, high, wall))))


# ---- holes --------------------------------------------------------------------------------
def _spot(b, where, tag, axis, value, what):
    """The expression for one coordinate: a new parameter, or the middle of the part."""
    if value is None:
        return where.middle(axis)
    return b.param(tag, what, value, "Distance")


def _deep(depth):
    return None if depth is None else _size(depth, "depth")


def _outside(where, axes, places, reach, what="hole"):
    """Raise when a cut is not fully in the part: its middle is outside, or its rim reaches a side
    (an open notch, not a hole). `places` are (u, v) numbers across the axis, `reach` is how far
    the cut goes from its place along the two axes (a hole: its radius)."""
    spans = "from %s %s and from %s %s" % (axes[0], where.span(axes[0]), axes[1], where.span(axes[1]))
    for place in places:
        at = "at %s %s, %s %s" % (axes[0], _text(place[0]), axes[1], _text(place[1]))
        if not all(where.inside(a, v) for a, v in zip(axes, place)):
            raise _Wrong("the %s %s is outside the part, which goes %s." % (what, at, spans))
        if not all(where.inside(a, v, far) for a, v, far in zip(axes, place, reach)):
            kind = what.split()[-1]
            goes = " and from ".join("%s %s to %s" % (a, _text(v - far), _text(v + far))
                                     for a, v, far in zip(axes, place, reach))
            middle = ", ".join("%s %s" % (a, _text(where.middle_number(a))) for a in axes)
            raise _Wrong("the %s %s breaks through the side of the part: the %s goes from %s, the part %s "
                         "(middle of the part: %s)." % (kind, at, kind, goes, spans, middle))


def _reach(b, where, tag, depth, axis):
    """(where a tool starts along the axis, how long it is), as the texts of expressions."""
    low, high = SIDES[axis]
    if depth is None:        # through: 1 mm longer than the part at both ends, so no face lies on a face
        return "%s - 1 mm" % where.side(low), "%s - (%s) + 2 mm" % (where.side(high), where.side(low))
    deep = b.param(tag, "Depth", depth)          # from the high side (the top) down
    return "%s - %s" % (where.side(high), deep), deep + " + 1 mm"


def _drill(b, base, where, tag, diameter, places, depth, axis):
    """Cut one round hole for each place into `base`. `diameter` and the two coordinates of a
    place (across the axis) are the texts of expressions."""
    axes = [a for a in "xyz" if a != axis]
    radius = diameter + " / 2"
    start, height = _reach(b, where, tag, depth, axis)
    tools = []
    for n, (u, v) in enumerate(places, 1):
        tool = b.add("Part::Cylinder", "%s_Tool%s" % (tag, n if len(places) > 1 else ""))
        tool.Placement = App.Placement(App.Vector(), TURN[axis])
        b.bind(tool, "Radius", radius)
        b.bind(tool, "Height", height)
        for letter, text in ((axis, start), (axes[0], u), (axes[1], v)):
            b.bind(tool, "Placement.Base." + letter, text)
        tools.append(tool)
    return b.cut(base, tools, tag)


def _holes(count, diameter, depth, axis):
    """"4 through holes d9 along z" for the printed line."""
    kind = "through hole" if depth is None else "hole"
    text = "%d %s%s d%s" % (count, kind, "" if count == 1 else "s", _text(diameter))
    return text + (" along %s" % axis if depth is None else ", %s deep from the high %s side" % (_text(depth), axis))


def _loose(where, more=()):
    """What the printed line names as not bound."""
    return list(more) + (["the sides of the part (the cut does not follow when the part changes)"]
                         if where.loose else [])


@_helper("cad.hole(plate, 9, 20, 30)")
def hole(part, diameter, x=None, y=None, depth=None, axis="z", name=None):
    """A round hole through the part along z. Without x and y it is in the middle of the part (a
    centre bore); with only one of them it is on the centre line. x can be a list of places.
    depth makes a blind hole from the top. With axis="x" the two numbers are y and z, with
    axis="y" they are x and z.

        plate = cad.hole(plate, 9, 20, 30)                     # 20 from the edge x = 0, 30 from y = 0
        disc = cad.hole(disc, 50)                               # centre bore
        plate = cad.hole(plate, 9, [(10, 10), (90, 10)])
    """
    base, given = _find(part)
    diameter, depth, axis = _size(diameter, "diameter"), _deep(depth), _axis(axis)
    axes = [a for a in "xyz" if a != axis]
    if isinstance(x, (list, tuple, App.Vector)):
        if y is not None:
            raise _Wrong("give the places as one list, or x and y as two numbers.")
        points = [_across(x, axis)] if _is_point(x) else [_across(p, axis) for p in x]
        if not points:
            raise _Wrong("the list of places is empty.")
    else:
        points = [tuple(None if v is None else _number(v, w) for v, w in ((x, axes[0]), (y, axes[1])))]
    if len(points) > MAX_HOLES:
        raise _Wrong("%d holes are too many for one call (at most %d)." % (len(points), MAX_HOLES))
    where = _Where(base)
    numbers = [tuple(where.middle_number(a) if v is None else v for a, v in zip(axes, p)) for p in points]
    _outside(where, axes, numbers, (diameter / 2, diameter / 2))
    with _Build(base.Document) as b:
        tag = b.tag(name, "Hole" if len(points) == 1 else "Holes")
        d, places = b.param(tag, "Diameter", diameter), []
        for n, point in enumerate(points, 1):
            number = "" if len(points) == 1 else str(n)
            places.append([_spot(b, where, tag, a, v, a.upper() + number) for a, v in zip(axes, point)])
        result = _drill(b, base, where, tag, d, places, depth, axis)
        if len(points) > 1:
            at = "at %d places" % len(points)
        elif points[0] == (None, None):
            at = "in the middle"
        else:
            at = "at %s %s, %s %s" % (axes[0], _text(numbers[0][0]), axes[1], _text(numbers[0][1]))
        return b.line(result, "%s %s in %s" % (_holes(len(points), diameter, depth, axis), at, _of(base, given)),
                      _loose(where))


@_helper("cad.hole_grid(plate, 9, 3, 2, 40, 50)")
def hole_grid(part, diameter, nx, ny=1, dx=None, dy=None, edge=None, center=None, depth=None, axis="z",
              name=None):
    """Holes in rows: nx holes along x with the pitch dx, in ny rows with the pitch dy. The pattern
    is in the middle of the part unless center=(x, y) is given. With edge= in place of the
    pitches the outer holes are that far from the edges of the part.

        plate = cad.hole_grid(plate, 9, 3, 2, 40, 50)       # 3 x 2 holes, pitch 40 and 50, centred
        plate = cad.hole_grid(plate, 9, 2, 2, edge=10)      # one hole in each corner, 10 from the edges
    """
    base, given = _find(part)
    diameter, depth, axis = _size(diameter, "diameter"), _deep(depth), _axis(axis)
    counts = [_count(nx, "number of holes along the first axis"), _count(ny, "number of rows")]
    if counts[0] * counts[1] > MAX_HOLES:
        raise _Wrong("%d holes are too many for one call (at most %d)." % (counts[0] * counts[1], MAX_HOLES))
    axes = [a for a in "xyz" if a != axis]
    pitches = [None if v is None else _size(v, "pitch") for v in (dx, dy)]
    if edge is not None:
        if pitches != [None, None] or center is not None:
            raise _Wrong("give the pitches or edge=, not both: with edge= the part gives the pitches.")
        edge = _size(edge, "distance from the edge")
    else:
        pitches = [p if p is not None else pitches[1 - i] for i, p in enumerate(pitches)]
        if any(n > 1 and p is None for n, p in zip(counts, pitches)):
            raise _Wrong("the pitch is missing: give dx and dy, or edge= for holes at the edges.")
    middle = None if center is None else _across(center, axis, "centre")
    where = _Where(base)

    def numbers(i):
        """The places of the holes along axis number i."""
        n, low, high = counts[i], where.number(SIDES[axes[i]][0]), where.number(SIDES[axes[i]][1])
        if edge is not None:
            if n > 1 and high - low <= 2 * edge:
                raise _Wrong("%s from both edges is more than the part has along %s (%s)."
                             % (_text(edge), axes[i], _text(high - low)))
            return [(low + high) / 2] if n == 1 else [low + edge + k * (high - low - 2 * edge) / (n - 1)
                                                      for k in range(n)]
        mid = (low + high) / 2 if middle is None else middle[i]
        return [mid + (k - (n - 1) / 2) * (pitches[i] or 0) for k in range(n)]

    _outside(where, axes, [(u, v) for u in numbers(0) for v in numbers(1)], (diameter / 2, diameter / 2))
    with _Build(base.Document) as b:
        tag = b.tag(name, "HoleGrid")
        d, texts = b.param(tag, "Diameter", diameter), []
        gap = b.param(tag, "Edge", edge) if edge is not None else ""
        for i, letter in enumerate(a.upper() for a in axes):
            n = counts[i]
            if edge is not None:
                low, high = where.side(SIDES[axes[i]][0]), where.side(SIDES[axes[i]][1])
                texts.append([where.middle(axes[i])] if n == 1 else [
                    "%s + %s + %.12g * (%s - (%s) - 2 * %s)" % (low, gap, k / (n - 1), high, low, gap)
                    for k in range(n)])
                continue
            mid = where.middle(axes[i]) if middle is None else b.param(tag, letter, middle[i], "Distance")
            pitch = b.param(tag, "Pitch" + letter, pitches[i]) if n > 1 else ""
            texts.append([mid if 2 * k == n - 1 else "%s %s %.12g * %s" % (
                mid, "-" if 2 * k < n - 1 else "+", abs(k - (n - 1) / 2), pitch) for k in range(n)])
        result = _drill(b, base, where, tag, d, [(u, v) for u in texts[0] for v in texts[1]], depth, axis)
        if edge is not None:
            how = "%s from the edges" % _text(edge)
        else:
            how = "pitch %s" % " and ".join(_text(p) for n, p in zip(counts, pitches) if n > 1) \
                if max(counts) > 1 else "one hole"
            how += ", centred on the part" if middle is None else ", centre at %s %s, %s %s" % (
                axes[0], _text(middle[0]), axes[1], _text(middle[1]))
        return b.line(result, "%s, %d along %s by %d along %s, %s, in %s" % (
            _holes(counts[0] * counts[1], diameter, depth, axis), counts[0], axes[0], counts[1], axes[1], how,
            _of(base, given)), _loose(where, ["the number of holes"]))


@_helper("cad.bolt_circle(disc, 11, 6, 90)")
def bolt_circle(part, diameter, count, circle, start=0, center=None, depth=None, axis="z", name=None):
    """Holes equally spaced on a circle: hole diameter, number of holes, diameter of the circle. The
    circle is in the middle of the part unless center=(x, y) is given. The first hole is at
    the angle `start`, measured from the x axis.

        disc = cad.bolt_circle(disc, 11, 6, 90)             # 6 holes d11 on a circle of 90
        plate = cad.bolt_circle(plate, 9, 4, 70, start=45)  # one hole towards each corner
    """
    base, given = _find(part)
    diameter, depth, axis = _size(diameter, "diameter"), _deep(depth), _axis(axis)
    count, circle, start = _count(count, "number of holes"), _size(circle, "diameter of the circle"), \
        _number(start, "start angle")
    if count > MAX_HOLES:
        raise _Wrong("%d holes are too many for one call (at most %d)." % (count, MAX_HOLES))
    axes = [a for a in "xyz" if a != axis]
    middle = None if center is None else _across(center, axis, "centre")
    where = _Where(base)
    mid = middle or [where.middle_number(a) for a in axes]
    angles = [start + k * 360.0 / count for k in range(count)]
    _outside(where, axes, [(mid[0] + circle / 2 * math.cos(math.radians(a)),
                            mid[1] + circle / 2 * math.sin(math.radians(a))) for a in angles],
             (diameter / 2, diameter / 2))
    with _Build(base.Document) as b:
        tag = b.tag(name, "BoltCircle")
        d = b.param(tag, "Diameter", diameter)
        radius = b.param(tag, "Circle", circle) + " / 2"
        first = b.param(tag, "Start", start, "Angle")
        centre = [where.middle(a) if middle is None else b.param(tag, a.upper(), middle[i], "Distance")
                  for i, a in enumerate(axes)]
        places = []
        for k in range(count):
            angle = first if k == 0 else "%s + %.12g deg" % (first, k * 360.0 / count)
            places.append(["%s + %s * %s(%s)" % (c, radius, turn, angle) for c, turn in zip(centre, ("cos", "sin"))])
        result = _drill(b, base, where, tag, d, places, depth, axis)
        return b.line(result, "%s on a circle of %s, the first at %s degrees, %s, in %s" % (
            _holes(count, diameter, depth, axis), _text(circle), _text(start),
            "centred on the part" if middle is None else "centre at %s %s, %s %s" % (
                axes[0], _text(middle[0]), axes[1], _text(middle[1])),
            _of(base, given)), _loose(where, ["the number of holes"]))


@_helper("cad.slot(bar, 9, 40, 85, 20)")
def slot(part, width, length, x=None, y=None, angle=0, depth=None, name=None):
    """A slot with round ends through the part along z: width, length overall, and x, y of its
    middle (without them: the middle of the part). It runs along x; angle turns it, 90 is along y.

        bar = cad.slot(bar, 9, 40, 85, 20)      # 9 wide, 40 long overall, its middle at x 85, y 20
    """
    base, given = _find(part)
    width, length, depth, angle = _size(width, "width"), _size(length, "length"), _deep(depth), \
        _number(angle, "angle")
    if length < width:
        width, length = length, width        # "slot 40 x 9": the long side is the length
    if length - width < TOL:
        raise _Wrong("a slot as long as it is wide is a round hole: use cad.hole(part, %s)." % _text(width))
    where = _Where(base)
    if isinstance(x, (list, tuple, App.Vector)) and y is None:
        x, y = _across(x, "z")               # cad.slot(bar, 9, 40, (85, 20))
    point = [None if v is None else _number(v, a) for v, a in ((x, "x"), (y, "y"))]
    numbers = [where.middle_number(a) if v is None else v for a, v in zip("xy", point)]
    # how far the turned slot goes from its middle along x and along y
    run, turned = (length - width) / 2, math.radians(angle)
    _outside(where, "xy", [numbers], (run * abs(math.cos(turned)) + width / 2, run * abs(math.sin(turned)) + width / 2),
             "middle of the slot")
    with _Build(base.Document) as b:
        tag = b.tag(name, "Slot")
        w, long = b.param(tag, "Width", width), b.param(tag, "Length", length)
        centre = [_spot(b, where, tag, a, v, a.upper()) for a, v in zip("xy", point)]
        turn = b.param(tag, "Angle", angle, "Angle")
        start, height = _reach(b, where, tag, depth, "z")
        # Built along x around the origin, then the fuse of the three is put into its place.
        half = "(%s - %s) / 2" % (long, w)
        ends = [b.add("Part::Cylinder", "%s_End%d" % (tag, n)) for n in (1, 2)]
        between = b.add("Part::Box", tag + "_Between")
        for end, sign in zip(ends, ("-", "")):
            b.bind(end, "Radius", w + " / 2")
            b.bind(end, "Placement.Base.x", sign + half)
        b.bind(between, "Length", "%s - %s" % (long, w))
        b.bind(between, "Width", w)
        b.bind(between, "Placement.Base.x", "-" + half)
        b.bind(between, "Placement.Base.y", "-%s / 2" % w)
        for obj in ends + [between]:
            b.bind(obj, "Height", height)
        tool = b.add("Part::MultiFuse", tag + "_Tool")
        tool.Shapes, tool.Refine = [ends[0], between, ends[1]], True
        tool.Placement = App.Placement(App.Vector(), App.Rotation(App.Vector(0, 0, 1), angle))
        for prop, text in (("Placement.Base.x", centre[0]), ("Placement.Base.y", centre[1]),
                           ("Placement.Base.z", start), ("Placement.Rotation.Angle", turn)):
            b.bind(tool, prop, text)
        b.hide(*ends, between)
        result = b.cut(base, [tool], tag, "slot")
        return b.line(result, "slot %s wide, %s long overall, %s, its middle at x %s, y %s, %s degrees from x, "
                              "in %s" % (_text(width), _text(length),
                                         "through along z" if depth is None else "%s deep from the top" % _text(depth),
                                         _text(numbers[0]), _text(numbers[1]), _text(angle), _of(base, given)),
                      _loose(where))


# ---- edges --------------------------------------------------------------------------------
def _sharp_edges(shape):
    """[(number, edge)] of the edges where two faces meet at an angle: no seam of a cylinder, no
    line where a flat face runs smoothly into a round one."""
    out = []
    for number, edge in enumerate(shape.Edges, 1):
        try:
            faces = shape.ancestorsOfType(edge, Part.Face)
            if len(faces) != 2 or edge.Length < TOL:
                continue
            point = edge.valueAt((edge.FirstParameter + edge.LastParameter) / 2)
            a, b = (f.normalAt(*f.Surface.parameter(point)) for f in faces)
            if abs(a.dot(b)) < 0.9995:
                out.append((number, edge))
        except Exception:
            pass
    return out


def _edges_of_face(shape, sharp, high):
    """The edges around the top (or the bottom) face of the part: its outline, not the holes in it."""
    box = _true_box(shape)
    level = box.ZMax if high else box.ZMin
    outline = []
    for face in shape.Faces:
        if abs(face.BoundBox.ZMin - level) < TOL and abs(face.BoundBox.ZMax - level) < TOL:
            outline += face.OuterWire.Edges
    return [(n, e) for n, e in sharp if any(e.isSame(o) for o in outline)]


def _pick_edges(shape, edges):
    """(numbers of the chosen edges, how the printed line names them)."""
    sharp = _sharp_edges(shape)
    choices = list(edges) if isinstance(edges, (list, tuple)) and not _is_point(edges) else [edges]
    chosen, words = {}, []
    for choice in choices:
        if _is_point(choice):
            if len(choice) != 3:
                raise _Wrong("a point near an edge is three numbers (x, y, z), not %r." % (choice,))
            vertex = Part.Vertex(App.Vector(*choice))
            found = sorted(sharp, key=lambda item: vertex.distToShape(item[1])[0])[:1]
            words.append("next to (%s)" % ", ".join(_text(v) for v in choice))
        else:
            word = str(choice).strip().lower()
            word = EDGE_WORDS.get(word, word)
            if word in SIDES:
                direction = App.Vector(*[float(a == word) for a in "xyz"])
                found = [(n, e) for n, e in sharp if e.Curve.TypeId == "Part::GeomLine"
                         and abs(abs(e.tangentAt(e.FirstParameter).dot(direction)) - 1) < 1e-6]
                words.append("parallel to " + word)
            elif word in ("top", "bottom"):
                found = _edges_of_face(shape, sharp, word == "top")
                words.append("around the %s face" % word)
            elif word == "all":
                found = sharp
                words.append("all")
            else:
                raise _Wrong('the edges are "x", "y" or "z" (all edges parallel to that axis), "top", '
                             '"bottom", "all", or a point (x, y, z) near one edge, not %r.' % (choice,))
        if not found:
            raise _Wrong('the part has no edge %s. The edges are "x", "y" or "z" (parallel to that axis), '
                         '"top", "bottom", "all", or a point (x, y, z) near one edge.' % words[-1])
        chosen.update(found)
    if not chosen:
        raise _Wrong("no edges were given.")
    return sorted(chosen), " and ".join(words)


def _edge_feature(kind, part, size, what, edges, name):
    base, given = _find(part)
    size = _size(size, what)
    numbers, words = _pick_edges(base.Shape, edges)
    pieces = len(_solids(base))
    with _Build(base.Document) as b:
        tag = b.tag(name, kind)
        result = b.add("Part::" + kind, tag)
        result.Base = base
        result.Edges = [(n, size, size) for n in numbers]
        b.name_after(result, base)
        b.hide(base)
        b.doc.recompute()
        problem = _problem(result)
        if problem or len(_solids(result)) != pieces:
            raise _Wrong("FreeCAD could not make a %s of %s on these %d edges%s. Use a smaller %s, or other edges."
                         % (kind.lower(), _text(size), len(numbers), " (%s)" % problem if problem else "", what))
        b.done(result)
        return b.line(result, "%s %s%s on %d edges (%s) of %s" % (
            kind.lower(), "r" if kind == "Fillet" else "", _text(size) + ("" if kind == "Fillet" else " x 45 degrees"),
            len(numbers), words, _of(base, given)), ["the %s (FreeCAD keeps it in %s.Edges)" % (what, result.Name)])


@_helper('cad.fillet(plate, 15, "z")')
def fillet(part, radius, edges="z", name=None):
    """Round edges of the part. The edges are chosen by where they are, never by number: "z" is
    all edges parallel to z (the four corners of a plate), also "x" and "y"; "top" and "bottom"
    are the edges around that face; "all"; a point (x, y, z) takes the edge next to it. A
    list takes several.

        plate = cad.fillet(plate, 15, "z")      # the four corners of a plate
        shaft = cad.fillet(shaft, 2, "top")
    """
    return _edge_feature("Fillet", part, radius, "radius", edges, name)


@_helper('cad.chamfer(plate, 10, "z")')
def chamfer(part, size, edges="z", name=None):
    """Chamfer edges of the part, size x 45 degrees. The edges are chosen as in cad.fillet: "x",
    "y", "z", "top", "bottom", "all", a point (x, y, z) near one edge, or a list of these.

        plate = cad.chamfer(plate, 10, "z")                 # the four corners cut off
        shaft = cad.chamfer(shaft, 1, ["top", "bottom"])    # both ends
    """
    return _edge_feature("Chamfer", part, size, "size", edges, name)


# ---- whole parts --------------------------------------------------------------------------
@_helper("cad.move(tube, 60, 50, 8)")
def move(part, x=0, y=0, z=0):
    """Put the part so that its origin is at x, y, z (not: move it by x, y, z). The origin of a
    plate is its corner, the origin of a shaft, a tube or a pipe is the middle of its lower end.

        tube = cad.move(tube, 60, 50, 8)        # stands at x 60, y 50 on a plate that is 8 thick
    """
    obj, given = _find(part)
    if isinstance(x, (list, tuple, App.Vector)):
        if len(x) != 3:
            raise _Wrong("the place is three numbers x, y, z, not %r." % (x,))
        x, y, z = x
    place = [_number(v, a) for v, a in ((x, "x"), (y, "y"), (z, "z"))]
    with _Build(obj.Document) as b:
        old = obj.Placement
        texts = dict(obj.ExpressionEngine)
        for letter, value in zip("xyz", place):
            prop = "Placement.Base." + letter
            b.back.append(functools.partial(obj.setExpression, prop, texts.get(prop) or texts.get("." + prop)))
            b.bind(obj, prop, b.param(obj.Name, "Pos" + letter.upper(), value, "Distance"))
        b.back.insert(0, functools.partial(setattr, obj, "Placement", old))
        b.done(obj)
        return b.line(obj, "moved, the origin of %s is now at (%s)" % (
            _of(obj, given), ", ".join(_text(v) for v in place)))


@_helper("cad.join(plate, tube)")
def join(*parts, name=None):
    """Fuse parts that touch into one solid, for example a tube on its base plate.

        post = cad.join(plate, tube)
    """
    found = []
    for part in _flat(parts):
        obj, given = _find(part)
        if obj.Name not in [o.Name for o, _ in found]:
            found.append((obj, given))
    if len(found) < 2:
        raise _Wrong("give two or more parts that are not yet one body.")
    objects = [o for o, _ in found]
    with _Build(objects[0].Document) as b:
        tag = b.tag(name, "Joined")
        result = b.add("Part::MultiFuse", tag, name)
        result.Shapes, result.Refine = objects, True
        b.hide(*objects)
        b.done(result)
        if len(_solids(result)) != 1:
            raise _Wrong("the parts do not touch: the result would be %d loose bodies. Put one in its place "
                         "first, for example cad.move(tube, 60, 50, 8)." % len(_solids(result)))
        return b.line(result, "one solid from %s" % " and ".join(_of(o, g) for o, g in found))


# Other names that a model reaches for: the same functions.
box = block = plate
disc = shaft
bore = holes = hole


def __getattr__(name):
    raise AttributeError("cad has no function %s. It has: %s. For every other shape write FreeCAD code "
                         "(Part, doc)." % (name, ", ".join(__all__)))
