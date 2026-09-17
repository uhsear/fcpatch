#!/usr/bin/env python
"""Apply a reviewed list of attribute corrections, and refuse every row whose
current value is no longer the value that was reviewed.

Sixteen rows are verified on Tuesday and applied on Thursday. Somebody else
fixed three of them in between. The Thursday batch writes the Tuesday values
back over that work, and on a class with editor tracking switched off the
overwrite leaves no stamp, so nobody finds out.

This reads a CSV of key, field, expected and new that a person reviewed, and
compares every expected value against what the row holds right now. An edit
whose row still agrees is planned. An edit whose row has drifted is reported as
a conflict and is not touched. An edit whose row already holds the new value
produces nothing at all, so a second run is a no-op.

The write is two-pass. Every planned row is read again and checked again before
the first updateRow call, so a row that drifted between the plan and the apply
stops the batch with nothing written, instead of stopping it half way through.

Why not the Field Calculator or an attribute rule. Field Calculator applies one
expression to a selection, which is the right tool for "uppercase this column",
and it has no idea what the value was when a person looked at it. An attribute
rule fires on edit and cannot express "only if this row still reads X". Neither
one refuses, and refusing is the whole job here.

    python fcpatch.py --self-test
    python fcpatch.py --fc prod.sde/Roads --edits reviewed.csv --key-field ASSETID
    python fcpatch.py --fc prod.sde/Roads --edits reviewed.csv --unique-field ASSETID
    python fcpatch.py --fc prod.sde/Roads --edits reviewed.csv --workspace prod.sde --apply

Exit codes: 0 the plan is clean or the apply succeeded, 1 a reviewed value no
longer matches, 2 the input could not be read or the apply failed, 64 usage
error.
"""

from __future__ import print_function

import argparse
import collections
import contextlib
import csv
import io
import json
import os
import shutil
import sys
import tempfile

# =============================================================================
# CONFIGURATION. Deliberately not flags. Change here, not at the call site.
# =============================================================================

# Token that means the database NULL, in the expected column or the new column.
# A CSV cell cannot hold a NULL, and an empty cell already means the empty
# string, which in a text column is a different value from NULL.
NULL_TOKEN = "<null>"

# Columns the reviewed CSV must carry. Any other column is carried along
# unread, so a reviewer can keep a notes or a ticket column in the same file.
EDIT_COLUMNS = ("key", "field", "expected", "new")

# Field the reviewed CSV keys rows by, unless --key-field says otherwise.
DEFAULT_KEY_FIELD = "OBJECTID"

# Conflicts and changes printed before the report truncates.
DEFAULT_SAMPLE = 20

# Column types this will write. Everything else is refused by name rather than
# attempted, because a wrong type reaches arcpy as a runtime error inside an
# open edit session, which is the worst place to discover a typo.
WRITABLE_TYPES = ("String", "Integer", "SmallInteger", "BigInteger", "Double",
                  "Single", "Float", "Guid", "GUID")

# Column types refused outright. OID and GlobalID are the database's identity
# for the row. Date has no unambiguous text form in a reviewed CSV. The rest
# are not attributes.
REFUSED_TYPES = ("OID", "GlobalID", "Geometry", "Blob", "Raster", "Date",
                 "DateOnly", "TimeOnly", "TimestampOffset")

# =============================================================================
# End of CONFIGURATION.
# =============================================================================

PRO_PYTHON = r"C:\Program Files\ArcGIS\Pro\bin\Python\envs\arcgispro-py3\python.exe"

ARCPY_MISSING = (
    "arcpy was not found. Run this with the Python that ships with ArcGIS Pro:\n"
    '  "%s" fcpatch.py --fc ... --edits ...\n'
    "or the propy.bat in ...\\Pro\\bin\\Python\\Scripts\\.\n"
    "Only --self-test runs without arcpy." % PRO_PYTHON)

# One reviewed line. `expected` and `new` are the raw cells, deliberately not
# stripped: a trailing space in a NAME column is exactly the kind of defect
# this tool is pointed at, and stripping it here would make it unfixable.
Edit = collections.namedtuple("Edit", "key field expected new line")

# One planned write. `old` is the live value read while the plan was built.
Change = collections.namedtuple("Change", "oid key field old new")

# One refused edit: the reviewed value is not what the row holds now.
Conflict = collections.namedtuple("Conflict", "oid key field found expected")

# The whole comparison. `applied` holds the edits that were already satisfied.
Plan = collections.namedtuple("Plan", "changes conflicts unmatched applied")


class UniqueCollision(ValueError):
    """A unique-field edit would move a value onto a row that does not own it."""


class PlanDrifted(RuntimeError):
    """A planned row changed between the plan and the write. Nothing was written."""

    def __init__(self, drifts):
        self.drifts = list(drifts)
        RuntimeError.__init__(
            self,
            "%d planned row(s) changed under us: %s -- nothing was written, "
            "re-run the check" % (
                len(self.drifts),
                "; ".join("OBJECTID %s %s found %r, expected %r"
                          % (d.oid, d.field, d.found, d.expected)
                          for d in self.drifts[:5])))


# ----------------------------------------------------------------- pure core

def _is_nan(value):
    """True for a float NaN.

    NaN is worth its own function because every comparison against it is false,
    including against itself. A NaN in a Double column would otherwise satisfy
    nothing and equal nothing, and the two halves of this tool would disagree
    about what that means. Here it means one thing: a NaN is never the value
    somebody reviewed, so an edit against it is always a conflict.
    """
    return isinstance(value, float) and value != value


def to_number(value):
    """value as a float, or None when it is not a plain finite number.

    nan, inf and 1e400 all survive float() and none of them is a value a
    reviewer wrote down, so all three read as not-a-number here.
    """
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def to_text(value):
    """The live value as the text a reviewer would have written for it."""
    if value is None:
        return NULL_TOKEN
    return value if isinstance(value, str) else str(value)


def same_value(current, text):
    """True when the live value equals the reviewed cell.

    A numeric column is compared as a number, so a Double reading 51.0 still
    matches a reviewed "51". A text column is compared as text and never as a
    number, because in a String field "051" and "51" are two different values
    and only one of them is in the row.
    """
    if _is_nan(current):
        return False
    if current is None:
        return text == NULL_TOKEN
    if text == NULL_TOKEN:
        return False
    if isinstance(current, (int, float)) and not isinstance(current, bool):
        number = to_number(text)
        return number is not None and float(current) == number
    return to_text(current) == text


def unchanged(a, b):
    """True when two values read from the layer are the same value.

    Used by the write-time re-check, where both sides come from the database
    rather than from the CSV. A NaN on either side is a change, for the reason
    in _is_nan: it is not the value the plan was built against.
    """
    if _is_nan(a) or _is_nan(b):
        return False
    if a is None or b is None:
        return a is None and b is None
    return a == b


def coerce(text, field_type):
    """Turn a reviewed cell into the value the column stores.

    A String column takes the cell verbatim, including its spaces. A numeric
    column parses it, and refuses here rather than inside the edit session.
    """
    if text == NULL_TOKEN:
        return None
    if field_type in ("Integer", "SmallInteger", "BigInteger"):
        number = to_number(text)
        if number is None or number != int(number):
            raise ValueError("%r is not a whole number, and the column is %s"
                             % (text, field_type))
        return int(number)
    if field_type in ("Double", "Single", "Float"):
        number = to_number(text)
        if number is None:
            raise ValueError("%r is not a number, and the column is %s"
                             % (text, field_type))
        return number
    return text


def parse_edits(raw_rows, header):
    """Edits from the rows of the reviewed CSV, in the order they were written.

    Two edits naming the same field of the same key are refused. They cannot
    both be right, and applying them in file order would silently pick one.
    """
    missing = [c for c in EDIT_COLUMNS if c not in header]
    if missing:
        raise ValueError("the edits CSV is missing the column(s) %s. It needs "
                         "%s." % (", ".join(missing), ", ".join(EDIT_COLUMNS)))
    edits = []
    seen = {}
    for offset, raw in enumerate(raw_rows):
        line = offset + 2                       # line 1 is the header
        cells = [raw.get(c) for c in EDIT_COLUMNS]
        if not any((c or "").strip() for c in cells):
            # Every spreadsheet leaves a trailing blank line behind. Refusing
            # the whole file over it would make the tool unusable from Excel.
            continue
        key = (raw.get("key") or "").strip()
        field = (raw.get("field") or "").strip()
        if not key:
            raise ValueError("line %d has no key" % line)
        if not field:
            raise ValueError("line %d has no field name" % line)
        if raw.get("expected") is None or raw.get("new") is None:
            raise ValueError("line %d is short of columns -- expected and new "
                             "are both required, use %s for a NULL"
                             % (line, NULL_TOKEN))
        pair = (key, field)
        if pair in seen:
            raise ValueError("line %d edits %s of key %r again, already edited "
                             "on line %d" % (line, field, key, seen[pair]))
        seen[pair] = line
        edits.append(Edit(key, field, raw["expected"], raw["new"], line))
    return edits


def check_fields(edits, field_types):
    """Refuse every edit naming a column that cannot be written.

    field_types maps column name to the arcpy field type. An unknown column, a
    Date and the OID all fail here, before an edit session is ever opened.
    """
    problems = []
    for edit in edits:
        if edit.field not in field_types:
            problems.append("line %d names the column %r, which the feature "
                            "class does not have" % (edit.line, edit.field))
            continue
        kind = field_types[edit.field]
        if kind in REFUSED_TYPES:
            problems.append("line %d edits %s, which is a %s column. fcpatch "
                            "writes attributes, not %s."
                            % (edit.line, edit.field, kind, kind))
        elif kind not in WRITABLE_TYPES:
            problems.append("line %d edits %s, whose type %s is not one "
                            "fcpatch knows how to write"
                            % (edit.line, edit.field, kind))
    if problems:
        raise ValueError("; ".join(problems))


def index_rows(rows, key_field):
    """Map each key value to the rows carrying it.

    Keys are compared as text, so a key field that is an Integer in the layer
    still matches the digits in the CSV.
    """
    index = {}
    for row in rows:
        if key_field not in row:
            raise ValueError("a row has no %s column, so it cannot be keyed"
                             % key_field)
        index.setdefault(to_text(row[key_field]), []).append(row)
    return index


def owners_of(rows, field):
    """Map each value of a unique field to the OBJECTIDs holding it."""
    owners = {}
    for row in rows:
        owners.setdefault(to_text(row.get(field)), set()).add(row["OBJECTID"])
    return owners


def near_miss(key, index):
    """A key in the layer that differs from this one only in whitespace.

    Worth reporting rather than matching. A trailing space in a NAME column is
    real data, and quietly matching through it would hide the thing the
    reviewer is most likely trying to fix.
    """
    for candidate in index:
        if candidate != key and candidate.strip() == key.strip():
            return candidate
    return None


def build_plan(edits, rows, key_field=DEFAULT_KEY_FIELD, field_types=None,
               unique_fields=()):
    """Compare the reviewed edits with the live rows.

    rows is a list of dicts, each carrying OBJECTID, the key field and every
    column an edit names. Returns a Plan.

    The order inside the loop is the guarantee. A row that already holds the
    new value is checked first and produces nothing, which is what makes a
    second run a no-op and lets a half-finished batch be re-run. Only then is
    the reviewed expected value compared, so a drifted row becomes a conflict
    rather than a write.
    """
    if field_types is not None:
        check_fields(edits, field_types)
    index = index_rows(rows, key_field)
    owners = dict((f, owners_of(rows, f)) for f in unique_fields)

    changes, conflicts, unmatched, applied = [], [], [], []
    for edit in edits:
        matches = index.get(edit.key, [])
        if not matches:
            unmatched.append((edit, near_miss(edit.key, index)))
            continue
        if len(matches) > 1:
            raise ValueError(
                "key %r matches OBJECTID %s -- %s is not unique, so line %d "
                "cannot name one row" % (edit.key,
                                         sorted(r["OBJECTID"] for r in matches),
                                         key_field, edit.line))
        row = matches[0]
        oid = row["OBJECTID"]
        if edit.field not in row:
            raise ValueError("line %d edits %s, which was not read from the "
                             "layer" % (edit.line, edit.field))
        current = row[edit.field]

        if same_value(current, edit.new):
            applied.append(Change(oid, edit.key, edit.field, current, current))
            continue
        if not same_value(current, edit.expected):
            conflicts.append(Conflict(oid, edit.key, edit.field,
                                      to_text(current), edit.expected))
            continue

        kind = (field_types or {}).get(edit.field, "String")
        try:
            new_value = coerce(edit.new, kind)
        except ValueError as exc:
            raise ValueError("line %d: %s" % (edit.line, exc))

        if edit.field in owners:
            held = owners[edit.field].get(to_text(new_value), set()) - set([oid])
            if held:
                # The self-collision case is the one that matters. A row that
                # already carries its new value is filtered out above, but a
                # row part way through a batch can still own the value it is
                # being given, and refusing that would make the tool
                # single-use. Only another OBJECTID is a collision.
                raise UniqueCollision(
                    "%s %s is already held by OBJECTID %s -- pick another "
                    "value for key %r on line %d"
                    % (edit.field, edit.new, sorted(held), edit.key, edit.line))
        changes.append(Change(oid, edit.key, edit.field, current, new_value))

    return Plan(changes, conflicts, unmatched, applied)


def verify_plan(changes, current):
    """Re-check every planned row against a fresh read. Returns the drifts.

    current maps OBJECTID to a dict of that row's fields. A row that has gone
    is a drift too: somebody deleted it between the review and the apply, and
    writing the other rows of that batch is not obviously the right answer.
    """
    drifts = []
    for change in changes:
        row = current.get(change.oid)
        if row is None:
            drifts.append(Conflict(change.oid, change.key, change.field,
                                   "<row is gone>", to_text(change.old)))
            continue
        if change.field not in row:
            raise ValueError("the re-read of OBJECTID %s carries no %s column"
                             % (change.oid, change.field))
        if not unchanged(row[change.field], change.old):
            drifts.append(Conflict(change.oid, change.key, change.field,
                                   to_text(row[change.field]),
                                   to_text(change.old)))
    return drifts


def changes_by_oid(changes):
    """Group the planned writes by row: {oid: {field: (old, new)}}."""
    grouped = {}
    for change in changes:
        row = grouped.setdefault(change.oid, {})
        if change.field in row:
            raise ValueError("two planned changes both write %s of OBJECTID %s"
                             % (change.field, change.oid))
        row[change.field] = (change.old, change.new)
    return grouped


def apply_rows(rows, fields, by_oid, update):
    """Write the planned values through a cursor.

    rows yields lists in `fields` order, with OBJECTID first, and update is the
    cursor's updateRow. The per-row re-check here is a backstop: apply_plan has
    already compared every row, so reaching this raise means the row moved
    between the two passes of the same edit session.
    """
    position = dict((name, i) for i, name in enumerate(fields))
    written = 0
    seen = set()
    for row in rows:
        oid = row[0]
        planned = by_oid.get(oid)
        if planned is None:
            continue
        for field, (old, new) in sorted(planned.items()):
            found = row[position[field]]
            if not unchanged(found, old):
                raise PlanDrifted([Conflict(oid, None, field, to_text(found),
                                            to_text(old))])
            row[position[field]] = new
        update(row)
        written += 1
        seen.add(oid)
    absent = sorted(set(by_oid) - seen)
    if absent:
        raise PlanDrifted([Conflict(oid, None, "OBJECTID", "<row is gone>",
                                    to_text(oid)) for oid in absent])
    return written


def apply_plan(changes, current, rows, fields, update):
    """Check every planned row, then write. No write at all when one drifted.

    This is the guarantee, and it is why the check and the write are two passes
    instead of one loop. Interleaving them writes rows 1 to 6 and then finds
    that row 7 moved. On a class without an edit session those six writes stay,
    and on one with an edit session the rollback depends on the abort landing.
    Checking everything first needs neither.
    """
    drifts = verify_plan(changes, current)
    if drifts:
        raise PlanDrifted(drifts)
    return apply_rows(rows, fields, changes_by_oid(changes), update)


def plan_fields(changes):
    """Cursor field list for a plan: OBJECTID first, then the edited columns."""
    return ["OBJECTID"] + sorted(set(c.field for c in changes))


def describe(plan, sample=DEFAULT_SAMPLE):
    """Render a plan as the lines the CLI prints."""
    out = []
    if plan.conflicts:
        out.append("%d CONFLICT(S) -- these rows changed since review, and are "
                   "not touched:" % len(plan.conflicts))
        for con in plan.conflicts[:sample]:
            out.append("  OBJECTID %s %s %s: found %r, reviewed %r"
                       % (con.oid, con.key, con.field, con.found, con.expected))
        if len(plan.conflicts) > sample:
            out.append("  ...and %d more" % (len(plan.conflicts) - sample))
        out.append("")
    if plan.unmatched:
        out.append("%d reviewed key(s) matched no row:" % len(plan.unmatched))
        for edit, miss in plan.unmatched[:sample]:
            note = "" if miss is None else (
                " -- the layer has %r, which differs only in whitespace" % miss)
            out.append("  line %d key %r%s" % (edit.line, edit.key, note))
        if len(plan.unmatched) > sample:
            out.append("  ...and %d more" % (len(plan.unmatched) - sample))
        out.append("")
    if plan.applied:
        out.append("%d reviewed edit(s) already hold the new value, and are "
                   "skipped." % len(plan.applied))
        out.append("")
    if not plan.changes:
        out.append("Nothing to write.")
        return out
    rows = len(set(c.oid for c in plan.changes))
    out.append("%d edit(s) across %d row(s):" % (len(plan.changes), rows))
    for change in plan.changes[:sample]:
        out.append("  [%4s] %-28s %-14s %r -> %r"
                   % (change.oid, str(change.key)[:28], change.field,
                      to_text(change.old), to_text(change.new)))
    if len(plan.changes) > sample:
        out.append("  ...and %d more" % (len(plan.changes) - sample))
    return out


# ------------------------------------------------------------------ geodatabase

_ARCPY_OVERRIDE = None


def _import_arcpy():
    """Import arcpy only when a real feature class is about to be read."""
    if _ARCPY_OVERRIDE is not None:
        return _ARCPY_OVERRIDE
    try:
        import arcpy
    except ImportError:
        raise SystemExit(ARCPY_MISSING)
    return arcpy


def field_types_of(arcpy, fc):
    """Column name to arcpy field type, for the whole feature class."""
    return dict((f.name, f.type) for f in arcpy.ListFields(fc))


def oid_where(oids):
    """A where clause selecting exactly these OBJECTIDs."""
    return "OBJECTID IN (%s)" % ",".join(str(o) for o in sorted(oids))


def read_rows(arcpy, fc, fields, where=None):
    """Every row of the feature class, as dicts, over the named fields."""
    with arcpy.da.SearchCursor(fc, fields, where) as cursor:
        return [dict(zip(fields, row)) for row in cursor]


def write_snapshot(path, rows):
    """Dump the pre-edit state of every touched row so a bad apply can be undone."""
    with open(path, "w") as handle:
        json.dump([dict((k, to_text(v)) for k, v in sorted(row.items()))
                   for row in rows], handle, indent=1, sort_keys=True)
    return path


def apply_with_arcpy(arcpy, fc, workspace, changes):
    """Run the two-pass apply against a real feature class.

    The edit session is opened first, so both passes see one consistent view of
    a versioned class and an abort can still undo a partial write. It is opened
    only when --workspace was given: a bare UpdateCursor cannot commit to a
    versioned class, and an Editor on a workspace that is not the one holding
    the class fails at startEditing rather than silently doing nothing.
    """
    fields = plan_fields(changes)
    where = oid_where(set(c.oid for c in changes))
    editor = arcpy.da.Editor(workspace) if workspace else None
    if editor is not None:
        editor.startEditing(False, True)
        editor.startOperation()
    try:
        current = dict((row["OBJECTID"], row)
                       for row in read_rows(arcpy, fc, fields, where))
        with arcpy.da.UpdateCursor(fc, fields, where) as cursor:
            written = apply_plan(changes, current, cursor, fields,
                                 cursor.updateRow)
        if editor is not None:
            editor.stopOperation()
            editor.stopEditing(True)
    except Exception:
        if editor is not None:
            editor.abortOperation()
            editor.stopEditing(False)
        raise
    return written


# ------------------------------------------------------------------ self-test

class _StubField(object):
    """The two attributes of an arcpy field object that fcpatch reads."""

    def __init__(self, name, kind):
        self.name = name
        self.type = kind


class _StubCursor(object):
    """arcpy.da.SearchCursor and UpdateCursor, in the twelve lines used here."""

    def __init__(self, table, fields, where, writable):
        self._table = table
        self._fields = list(fields)
        self._oids = table.selected(where)
        self._writable = writable
        self._row = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        for oid in self._oids:
            self._row = oid
            yield [self._table.rows[oid][f] for f in self._fields]

    def updateRow(self, row):
        if not self._writable:
            raise RuntimeError("a SearchCursor cannot update")
        if self._table.fail_write:
            raise RuntimeError("the database refused the write")
        self._table.updates += 1
        for name, value in zip(self._fields, row):
            self._table.rows[self._row][name] = value


class _StubEditor(object):
    """arcpy.da.Editor, recording the calls rather than performing them."""

    def __init__(self, table, workspace):
        self._table = table
        self.workspace = workspace
        table.editor_calls.append(("Editor", workspace))

    def _record(self, name):
        self._table.editor_calls.append((name, None))

    def startEditing(self, with_undo, multiuser):
        self._record("startEditing")

    def startOperation(self):
        self._record("startOperation")

    def stopOperation(self):
        self._record("stopOperation")

    def abortOperation(self):
        self._record("abortOperation")
        self._table.rows = dict((oid, dict(row))
                                for oid, row in self._table.before.items())

    def stopEditing(self, save):
        self._record("stopEditing(%s)" % save)


class _StubDa(object):
    """The arcpy.da namespace."""

    def __init__(self, table):
        self._table = table

    def SearchCursor(self, fc, fields, where=None):
        return _StubCursor(self._table, fields, where, False)

    def UpdateCursor(self, fc, fields, where=None):
        return _StubCursor(self._table, fields, where, True)

    def Editor(self, workspace):
        return _StubEditor(self._table, workspace)


class _StubArcpy(object):
    """A feature class in a dict, with the arcpy surface fcpatch touches.

    Used only by --self-test. It exists so the --apply path, the edit session
    and the two-pass write are exercised for real with no Esri software
    present, rather than only described in the README.
    """

    FC = "stub.gdb/Assets"

    def __init__(self, rows, types, hook=None, hook_at=0):
        self.rows = dict((row["OBJECTID"], dict(row)) for row in rows)
        self.before = dict((oid, dict(row)) for oid, row in self.rows.items())
        self.types = dict(types)
        self.updates = 0
        self.opened = 0
        self.fail_write = False
        self.editor_calls = []
        # Every where clause any cursor was opened with, in order. The apply
        # must re-read only the rows it plans to write, not the whole class.
        self.wheres = []
        # hook runs as the hook_at'th cursor opens, which is how a concurrent
        # edit by somebody else is simulated at an exact point in the run.
        self.hook = hook
        self.hook_at = hook_at
        self.da = _StubDa(self)

    def selected(self, where):
        self.opened += 1
        self.wheres.append(where)
        if self.hook is not None and self.opened == self.hook_at:
            self.hook(self)
        oids = sorted(self.rows)
        if where:
            wanted = set(int(t) for t in
                         where.split("(")[1].split(")")[0].split(","))
            oids = [o for o in oids if o in wanted]
        return oids

    def Exists(self, path):
        return path in (self.FC, "stub.gdb")

    def ListFields(self, fc):
        return [_StubField(n, t) for n, t in sorted(self.types.items())]


def _stub_table():
    """Ten rows keyed by ASSETID, the fixture every stub run starts from."""
    rows = []
    for i in range(1, 11):
        rows.append({"OBJECTID": i, "ASSETID": "A%02d" % i,
                     "CONDITION": "fair", "LANES": 2, "OWNER": "county"})
    rows[6]["CONDITION"] = "poor"          # OBJECTID 7, the row that drifts
    return rows


STUB_TYPES = {"OBJECTID": "OID", "ASSETID": "String", "CONDITION": "String",
              "LANES": "SmallInteger", "OWNER": "String", "BUILT": "Date"}


def self_test():
    """Assertions over the decision core. No arcpy, no database, no network."""
    passed = [0]
    failed = []

    def check(cond, label):
        if cond:
            passed[0] += 1
            print("PASS  %s" % label)
        else:
            failed.append(label)
            print("FAIL  %s" % label)

    def raises(fn, label, kind=ValueError):
        try:
            fn()
        except kind:
            check(True, label)
        except Exception as exc:
            check(False, "%s (wrong exception %r)" % (label, exc))
        else:
            check(False, "%s (no error raised)" % label)

    def edit(key, field, expected, new, line=2):
        return Edit(key, field, expected, new, line)

    def rows(n=3, over=None):
        out = []
        for i in range(1, n + 1):
            row = {"OBJECTID": i, "NAME": "row %d" % i, "VALUE": "old",
                   "COUNT": 1}
            row.update((over or {}).get(i, {}))
            out.append(row)
        return out

    print("fcpatch self-test: no arcpy, no database, no network")
    print("-" * 68)

    # ---- the headline: a row that drifted since review is never written
    live = rows(3, {2: {"VALUE": "somebody else fixed it"}})
    plan = build_plan([edit("row 1", "VALUE", "old", "new"),
                       edit("row 2", "VALUE", "old", "new", line=3),
                       edit("row 3", "VALUE", "old", "new", line=4)],
                      live, key_field="NAME")
    check(len(plan.conflicts) == 1,
          "a mismatch against the reviewed value is a conflict  <-- pinned defect")
    check(len(plan.changes) == 2,
          "a conflict on one row does not suppress another row's edit")
    check(plan.conflicts[0].oid == 2, "the conflict names the row it refused")
    check(plan.conflicts[0].found == "somebody else fixed it",
          "the conflict reports what the row holds now")
    check(plan.conflicts[0].expected == "old",
          "the conflict reports what was reviewed")
    check([c.oid for c in plan.changes] == [1, 3],
          "the two rows that still agree are both planned")

    # ---- idempotence: a second run plans nothing
    done = rows(3, {1: {"VALUE": "new"}, 2: {"VALUE": "new"},
                    3: {"VALUE": "new"}})
    plan = build_plan([edit("row 1", "VALUE", "old", "new"),
                       edit("row 2", "VALUE", "old", "new", line=3),
                       edit("row 3", "VALUE", "old", "new", line=4)],
                      done, key_field="NAME")
    check(plan.changes == [],
          "an edit whose row already holds the new value plans nothing")
    check(len(plan.applied) == 3, "all three are reported as already applied")
    check(plan.conflicts == [],
          "an already-applied edit is not a conflict, so a second run is a no-op")
    half = rows(3, {1: {"VALUE": "new"}})
    plan = build_plan([edit("row 1", "VALUE", "old", "new"),
                       edit("row 2", "VALUE", "old", "new", line=3)],
                      half, key_field="NAME")
    check(len(plan.changes) == 1 and len(plan.applied) == 1,
          "a half-finished batch re-runs and finishes the rest")

    # ---- the order inside build_plan, which is what makes that work
    plan = build_plan([edit("row 1", "VALUE", "something else", "new")],
                      rows(1, {1: {"VALUE": "new"}}), key_field="NAME")
    check(plan.applied and not plan.conflicts,
          "the already-correct test runs before the expected test")

    # ---- unique fields
    unique = [{"OBJECTID": 1, "NAME": "a", "FACILITYID": "46"},
              {"OBJECTID": 9, "NAME": "b", "FACILITYID": "51"}]

    def collide():
        return build_plan([edit("a", "FACILITYID", "46", "51")], unique,
                          key_field="NAME", unique_fields=("FACILITYID",))
    raises(collide, "a unique value held by another row is refused",
           UniqueCollision)
    try:
        collide()
    except UniqueCollision as exc:
        message = str(exc)
    check(message.startswith("FACILITYID 51 is already held by OBJECTID [9]"),
          "the refusal names the field, the value and the OBJECTID holding it")
    check("pick another" in message, "the refusal says what to do about it")
    check(issubclass(UniqueCollision, ValueError),
          "the collision is a ValueError, so a caller need not know the subclass")

    self_held = [{"OBJECTID": 1, "NAME": "a", "FACILITYID": "51"}]
    plan = build_plan([edit("a", "FACILITYID", "46", "51")], self_held,
                      key_field="NAME", unique_fields=("FACILITYID",))
    check(plan.changes == [] and len(plan.applied) == 1,
          "a row already holding its own new value does not collide with itself"
          "  <-- pinned defect")

    # The check above is spared by the already-applied test, one branch earlier,
    # so on its own it says nothing about the self-exclusion in the owner set.
    # The pair below reaches that subtraction: the reviewed cell and the stored
    # value are the same value written two ways, so the row owns the value it is
    # being given while the edit is still a real edit. Only the OBJECTID holding
    # the value differs between the two cases.
    int_types = {"OBJECTID": "OID", "NAME": "String", "FACILITYID": "Integer"}
    own = [{"OBJECTID": 1, "NAME": "a", "FACILITYID": "51"}]
    plan = build_plan([edit("a", "FACILITYID", "51", "51.0")], own,
                      key_field="NAME", field_types=int_types,
                      unique_fields=("FACILITYID",))
    check([c.new for c in plan.changes] == [51],
          "a row owning the value it is being given is planned, not refused"
          "  <-- pinned defect")
    other = [{"OBJECTID": 1, "NAME": "a", "FACILITYID": "46"},
             {"OBJECTID": 9, "NAME": "b", "FACILITYID": "51"}]
    raises(lambda: build_plan([edit("a", "FACILITYID", "46", "51.0")], other,
                              key_field="NAME", field_types=int_types,
                              unique_fields=("FACILITYID",)),
           "and the same edit is refused when another OBJECTID owns it",
           UniqueCollision)
    free = [{"OBJECTID": 1, "NAME": "a", "FACILITYID": "46"},
            {"OBJECTID": 9, "NAME": "b", "FACILITYID": "47"}]
    plan = build_plan([edit("a", "FACILITYID", "46", "51")], free,
                      key_field="NAME", unique_fields=("FACILITYID",))
    check(len(plan.changes) == 1, "an unheld unique value is planned normally")
    plan = build_plan([edit("a", "FACILITYID", "46", "51")], free,
                      key_field="NAME")
    check(len(plan.changes) == 1,
          "without --unique-field the collision check does not run at all")

    def swap():
        return build_plan([edit("a", "FACILITYID", "46", "47"),
                           edit("b", "FACILITYID", "47", "46", line=3)],
                          free, key_field="NAME",
                          unique_fields=("FACILITYID",))
    raises(swap, "a straight swap of two unique values is refused, not half done",
           UniqueCollision)

    # ---- THE PINNED DEFECT: check every row before writing any row
    ten = [{"OBJECTID": i, "NAME": "r%d" % i, "VALUE": "old"}
           for i in range(1, 11)]
    plan = build_plan([edit("r%d" % i, "VALUE", "old", "new", line=i + 1)
                       for i in range(1, 11)], ten, key_field="NAME")
    check(len(plan.changes) == 10, "ten rows are planned")
    fields = plan_fields(plan.changes)
    check(fields == ["OBJECTID", "VALUE"],
          "the cursor field list is OBJECTID first, then the edited columns")

    drifted = dict((row["OBJECTID"], dict(row)) for row in ten)
    drifted[7]["VALUE"] = "somebody else fixed it"
    cursor_rows = [[row["OBJECTID"], row["VALUE"]] for row in ten]
    writes = []
    raises(lambda: apply_plan(plan.changes, drifted, cursor_rows, fields,
                             writes.append),
           "row 7 of 10 drifting aborts the whole apply", PlanDrifted)
    check(writes == [],
          "a drift at row 7 of 10 writes ZERO rows  <-- pinned defect")

    clean = dict((row["OBJECTID"], dict(row)) for row in ten)
    writes = []
    written = apply_plan(plan.changes, clean, cursor_rows, fields,
                         writes.append)
    check(written == 10, "an undrifted plan writes every row")
    check(len(writes) == 10, "the cursor sees ten updateRow calls")
    check(all(row[1] == "new" for row in writes),
          "every written row carries the new value")

    # ---- verify_plan on its own
    check(verify_plan(plan.changes, clean) == [],
          "an unchanged read produces no drift")
    drifts = verify_plan(plan.changes, drifted)
    check(len(drifts) == 1 and drifts[0].oid == 7, "the drift names row 7")
    check(drifts[0].found == "somebody else fixed it",
          "the drift reports the value found")
    gone = dict((oid, row) for oid, row in clean.items() if oid != 4)
    drifts = verify_plan(plan.changes, gone)
    check(len(drifts) == 1 and drifts[0].found == "<row is gone>",
          "a row deleted between the plan and the apply is a drift")
    raises(lambda: verify_plan(plan.changes, {7: {"OBJECTID": 7}}),
           "a re-read missing the edited column raises rather than passing")

    exc = PlanDrifted(drifts)
    check("nothing was written" in str(exc),
          "the drift error says nothing was written")
    check(len(exc.drifts) == 1, "the drift error carries the drifts")
    many = PlanDrifted([Conflict(i, None, "V", "a", "b") for i in range(9)])
    check(len(many.drifts) == 9,
          "the drift error keeps every drift even though it prints only five")
    check(str(many).count("OBJECTID") == 5,
          "and prints exactly the first five of them")

    # ---- apply_rows on its own, including the write-time backstop
    by_oid = changes_by_oid(plan.changes)
    check(len(by_oid) == 10, "the plan groups into ten rows")
    check(by_oid[3] == {"VALUE": ("old", "new")},
          "each row carries its field, its old value and its new value")
    raises(lambda: changes_by_oid([Change(1, "a", "V", "x", "y"),
                                   Change(1, "a", "V", "x", "z")]),
           "two planned writes to one field of one row raise")
    moved = [[row["OBJECTID"], row["VALUE"]] for row in ten]
    moved[6][1] = "moved after the pre-flight"
    writes = []
    raises(lambda: apply_rows(moved, fields, by_oid, writes.append),
           "the write-time re-check still catches a row that moved", PlanDrifted)
    check(len(writes) == 6,
          "the backstop stops at the row that moved, having written six")
    short = [[row["OBJECTID"], row["VALUE"]] for row in ten if row["OBJECTID"] != 5]
    writes = []
    raises(lambda: apply_rows(short, fields, by_oid, writes.append),
           "a planned row the cursor never returned raises", PlanDrifted)
    spare = [[99, "old"]] + [[row["OBJECTID"], row["VALUE"]] for row in ten]
    writes = []
    check(apply_rows(spare, fields, by_oid, writes.append) == 10,
          "a row the plan does not name is skipped, not written")

    # ---- the NaN policy, asserted on its own function. same_value and
    # unchanged are each independently NaN-safe, so a broken _is_nan can hide
    # behind them. These pin the policy directly.
    check(_is_nan(float("nan")) is True, "a float NaN is a NaN")
    check(_is_nan(float("inf")) is False,
          "an infinity is not a NaN, it is a number nobody reviewed")
    check(_is_nan(51.0) is False, "an ordinary float is not a NaN")
    check(_is_nan(51) is False, "an int is not a NaN")
    check(_is_nan("nan") is False,
          "the text 'nan' is a text value, not a NaN  <-- pinned defect")
    check(_is_nan(None) is False, "a NULL is not a NaN")

    # ---- value comparison, which every decision above rests on
    check(same_value("old", "old"), "a text value matches its reviewed text")
    check(not same_value("old", "Old"), "the comparison is case sensitive")
    check(not same_value("old ", "old"),
          "a trailing space is a difference, not whitespace to ignore")
    check(same_value(None, NULL_TOKEN), "a NULL matches the null token")
    check(not same_value(None, ""),
          "a NULL does not match an empty cell  <-- pinned defect")
    check(not same_value("", NULL_TOKEN),
          "an empty string does not match the null token")
    check(same_value("", ""), "an empty string matches an empty cell")
    check(same_value(51, "51"), "an Integer column matches its digits")
    check(same_value(51.0, "51"),
          "a Double reading 51.0 matches a reviewed 51  <-- pinned defect")
    check(same_value(51.0, "51.0"), "and matches 51.0 as well")
    check(not same_value("051", "51"),
          "a String column is never compared as a number")
    check(not same_value(51, "51x"), "trailing rubbish does not match a number")
    check(not same_value(float("nan"), "nan"),
          "a NaN never matches the value somebody reviewed  <-- pinned defect")
    check(not same_value(float("inf"), "inf"), "nor does an infinity")
    check(not same_value(True, "1"),
          "a Boolean is not silently read as the number 1")

    check(unchanged("a", "a"), "two equal reads are unchanged")
    check(unchanged(None, None), "two NULLs are unchanged")
    check(not unchanged(None, ""), "a NULL and an empty string differ")
    check(not unchanged("", None), "and differ the other way round")
    check(not unchanged(float("nan"), float("nan")),
          "two NaNs are never the same value  <-- pinned defect")
    check(not unchanged(1, float("nan")), "a NaN against a number is a change")

    check(to_number("51") == 51.0, "a number parses")
    check(to_number(" 51 ") == 51.0, "surrounding spaces do not stop it")
    check(to_number("nan") is None, "nan is not a number here")
    check(to_number("inf") is None, "inf is not a number here")
    check(to_number("1e400") is None, "an overflow to infinity is not a number")
    check(to_number("abc") is None, "text is not a number")
    check(to_number(None) is None, "None is not a number")
    check(to_number(True) is None, "True is not the number 1")
    check(to_text(None) == NULL_TOKEN, "a NULL renders as the null token")
    check(to_text(51) == "51", "an integer renders as its digits")
    check(to_text("a") == "a", "text renders as itself")

    # ---- coercing the reviewed cell to the column's type
    check(coerce("51", "String") == "51", "a String column takes the cell")
    check(coerce(" 51 ", "String") == " 51 ",
          "a String column keeps the spaces the reviewer typed")
    check(coerce("51", "Integer") == 51, "an Integer column takes a whole number")
    check(isinstance(coerce("51", "Integer"), int),
          "and takes it as an int, not a float")
    check(coerce("51.5", "Double") == 51.5, "a Double column takes a decimal")
    check(coerce(NULL_TOKEN, "String") is None, "the null token becomes None")
    check(coerce(NULL_TOKEN, "Integer") is None,
          "the null token becomes None on a numeric column too")
    raises(lambda: coerce("51.5", "Integer"),
           "a decimal into an Integer column raises")
    raises(lambda: coerce("abc", "Double"), "text into a Double column raises")
    raises(lambda: coerce("nan", "Double"),
           "a NaN into a Double column raises  <-- pinned defect")

    # ---- schema checking, before any edit session is opened
    types = {"OBJECTID": "OID", "NAME": "String", "VALUE": "String",
             "COUNT": "Integer", "BUILT": "Date", "SHAPE": "Geometry"}
    check(check_fields([edit("a", "VALUE", "x", "y")], types) is None,
          "a String column passes the schema check")
    raises(lambda: check_fields([edit("a", "NOPE", "x", "y")], types),
           "a column the layer does not have raises")
    raises(lambda: check_fields([edit("a", "BUILT", "x", "y")], types),
           "a Date column is refused rather than written")
    raises(lambda: check_fields([edit("a", "OBJECTID", "1", "2")], types),
           "editing the OID is refused  <-- pinned defect")
    raises(lambda: check_fields([edit("a", "SHAPE", "x", "y")], types),
           "editing the geometry is refused")
    raises(lambda: check_fields([edit("a", "VALUE", "x", "y")],
                                {"VALUE": "Xml"}),
           "a type fcpatch does not know is refused, not attempted")
    try:
        check_fields([edit("a", "BUILT", "x", "y"),
                      edit("b", "NOPE", "x", "y", line=3)], types)
    except ValueError as exc:
        both = str(exc)
    check("BUILT" in both and "NOPE" in both,
          "every schema problem is reported, not just the first")

    # ---- reading the reviewed CSV
    header = list(EDIT_COLUMNS)
    edits = parse_edits([{"key": "a", "field": "V", "expected": "x", "new": "y"}],
                        header)
    check(len(edits) == 1 and edits[0].line == 2,
          "the first data row is line 2, because line 1 is the header")
    edits = parse_edits([{"key": " a ", "field": " V ", "expected": " x ",
                          "new": " y "}], header)
    check(edits[0].key == "a" and edits[0].field == "V",
          "the key and the field name are stripped")
    check(edits[0].expected == " x " and edits[0].new == " y ",
          "the values are NOT stripped  <-- pinned defect")
    edits = parse_edits([{"key": "", "field": "", "expected": "", "new": ""},
                         {"key": "a", "field": "V", "expected": "x",
                          "new": "y"}], header)
    check(len(edits) == 1 and edits[0].line == 3,
          "a blank line is skipped and the line numbers still count it")
    edits = parse_edits([{"key": "a", "field": "V", "expected": "x", "new": "y",
                          "ticket": "GIS-41"}], header + ["ticket"])
    check(len(edits) == 1, "an extra column a reviewer added is carried unread")
    raises(lambda: parse_edits([], ["key", "field", "expected"]),
           "a CSV missing the new column raises")
    raises(lambda: parse_edits([{"key": "", "field": "V", "expected": "x",
                                 "new": "y"}], header),
           "a row with no key raises")
    raises(lambda: parse_edits([{"key": "a", "field": "", "expected": "x",
                                 "new": "y"}], header),
           "a row with no field name raises")
    raises(lambda: parse_edits([{"key": "a", "field": "V", "expected": "x",
                                 "new": None}], header),
           "a row short of columns raises")
    raises(lambda: parse_edits([{"key": "a", "field": "V", "expected": "x",
                                 "new": "y"},
                                {"key": "a", "field": "V", "expected": "y",
                                 "new": "z"}], header),
           "two edits to one field of one key raise  <-- pinned defect")
    edits = parse_edits([{"key": "a", "field": "V", "expected": "x", "new": "y"},
                         {"key": "a", "field": "W", "expected": "x",
                          "new": "y"}], header)
    check(len(edits) == 2, "two edits to different fields of one key are fine")

    # ---- keys
    check(index_rows(rows(2), "NAME")["row 1"][0]["OBJECTID"] == 1,
          "rows index by their key field")
    check(list(index_rows([{"OBJECTID": 1, "K": 51}], "K")) == ["51"],
          "a numeric key field indexes as text")
    raises(lambda: index_rows([{"OBJECTID": 1}], "NAME"),
           "a row with no key column raises")
    dupes = [{"OBJECTID": 1, "NAME": "same", "VALUE": "old"},
             {"OBJECTID": 2, "NAME": "same", "VALUE": "old"}]
    raises(lambda: build_plan([edit("same", "VALUE", "old", "new")], dupes,
                              key_field="NAME"),
           "a key matching two rows raises rather than picking one"
           "  <-- pinned defect")
    plan = build_plan([edit("missing", "VALUE", "old", "new")], rows(2),
                      key_field="NAME")
    check(len(plan.unmatched) == 1 and plan.unmatched[0][1] is None,
          "a key matching no row is unmatched, with no near miss")
    spaced = [{"OBJECTID": 1, "NAME": "row 1 ", "VALUE": "old"}]
    plan = build_plan([edit("row 1", "VALUE", "old", "new")], spaced,
                      key_field="NAME")
    check(plan.unmatched[0][1] == "row 1 ",
          "a key differing only in whitespace is reported, not matched"
          "  <-- pinned defect")
    check(near_miss("a", {"a": [], "b": []}) is None,
          "an exact key is not its own near miss")
    plan = build_plan([edit("1", "VALUE", "old", "new")], rows(2))
    check(len(plan.changes) == 1,
          "the default key field is OBJECTID, matched as text")
    raises(lambda: build_plan([edit("row 1", "GONE", "old", "new")], rows(1),
                              key_field="NAME"),
           "an edit naming a column that was not read raises")

    # ---- unique owners
    owners = owners_of([{"OBJECTID": 1, "F": "a"}, {"OBJECTID": 2, "F": "a"},
                        {"OBJECTID": 3, "F": None}], "F")
    check(owners["a"] == set([1, 2]), "two rows sharing a value both own it")
    check(owners[NULL_TOKEN] == set([3]), "a NULL is an owner group of its own")

    # ---- the report
    lines = describe(build_plan([edit("row 1", "VALUE", "old", "new")],
                                rows(2), key_field="NAME"))
    check(any("1 edit(s) across 1 row(s)" in l for l in lines),
          "a plan reports its edit and row counts")
    check(any("'old' -> 'new'" in l for l in lines),
          "each planned edit shows the old and the new value")
    lines = describe(build_plan([], rows(2), key_field="NAME"))
    check(lines == ["Nothing to write."], "an empty plan says so and stops")
    big = [{"OBJECTID": i, "NAME": "r%d" % i, "VALUE": "old"}
           for i in range(1, 31)]
    lines = describe(build_plan([edit("r%d" % i, "VALUE", "old", "new",
                                      line=i + 1) for i in range(1, 31)],
                                big, key_field="NAME"), sample=5)
    check(any("...and 25 more" in l for l in lines),
          "a long plan truncates and says how many it hid")
    conflicting = [{"OBJECTID": i, "NAME": "r%d" % i, "VALUE": "moved"}
                   for i in range(1, 31)]
    lines = describe(build_plan([edit("r%d" % i, "VALUE", "old", "new",
                                      line=i + 1) for i in range(1, 31)],
                                conflicting, key_field="NAME"), sample=5)
    check(any("30 CONFLICT(S)" in l for l in lines), "conflicts are counted")
    check(len([l for l in lines if l.startswith("  OBJECTID")]) == 5,
          "the conflict list truncates to the sample size")
    lines = describe(build_plan([edit("nope%d" % i, "VALUE", "old", "new",
                                      line=i + 1) for i in range(1, 31)],
                                big, key_field="NAME"), sample=5)
    check(any("30 reviewed key(s) matched no row" in l for l in lines),
          "unmatched keys are counted")
    check(any("...and 25 more" in l for l in lines),
          "the unmatched list truncates too")
    lines = describe(build_plan([edit("r1", "VALUE", "old", "old")], big,
                                key_field="NAME"))
    check(any("already hold the new value" in l for l in lines),
          "the already-applied count is reported")

    # ---- the where clause and the cursor field list
    check(oid_where([3, 1, 2]) == "OBJECTID IN (1,2,3)",
          "the where clause sorts the OBJECTIDs")
    check(plan_fields([Change(1, "a", "B", "x", "y"),
                       Change(2, "b", "A", "x", "y")])
          == ["OBJECTID", "A", "B"],
          "the cursor field list is OBJECTID then the columns, sorted")

    # ---- argument handling
    args = _parse(["--fc", "f", "--edits", "e.csv"])
    check(args.apply is False, "--apply defaults to OFF")
    check(args.key_field == DEFAULT_KEY_FIELD,
          "--key-field defaults to the configured value")
    check(args.unique_field == [], "--unique-field defaults to empty")
    check(args.workspace is None, "--workspace defaults to none")
    check(args.snapshot is None, "--snapshot defaults to none")
    check(args.sample == DEFAULT_SAMPLE, "--sample defaults to the configured value")
    check(_parse(["--self-test"]).self_test, "--self-test parses")
    check(_parse(["--fc", "f", "--edits", "e.csv",
                  "--key-field", "ASSETID"]).key_field == "ASSETID",
          "--key-field is read")
    check(_parse(["--fc", "f", "--edits", "e.csv", "--unique-field", "A",
                  "--unique-field", "B"]).unique_field == ["A", "B"],
          "--unique-field repeats")
    check(_parse(["--fc", "f", "--edits", "e.csv",
                  "--workspace", "w.sde"]).workspace == "w.sde",
          "--workspace is read")
    check(_parse(["--fc", "f", "--edits", "e.csv",
                  "--snapshot", "s.json"]).snapshot == "s.json",
          "--snapshot is read")
    check(_parse(["--fc", "f", "--edits", "e.csv", "--sample", "3"]).sample == 3,
          "--sample is read")
    check(_parse(["--fc", "f", "--edits", "e.csv", "--apply"]).apply is True,
          "--apply is read")

    # ---- arcpy is imported lazily, and says what to do when it is absent
    try:
        import arcpy
        have_arcpy = arcpy is not None
    except ImportError:
        have_arcpy = False
    if have_arcpy:
        check(_import_arcpy() is not None,
              "arcpy is returned when ArcGIS Pro's Python is running this")
    else:
        raises(_import_arcpy,
               "a missing arcpy names the Pro python instead of a traceback",
               SystemExit)

    # ---- real files on disk, and the whole CLI against a stub feature class.
    # Everything below writes into one temp directory and deletes it again.
    tmp = tempfile.mkdtemp(prefix="fcpatch-selftest-")

    def tmpfile(name, text, encoding="utf-8"):
        path = os.path.join(tmp, name)
        with open(path, "w", newline="", encoding=encoding) as handle:
            handle.write(text)
        return path

    def run_cli(argv, stub=None):
        """main() with its output captured and a stub arcpy installed."""
        global _ARCPY_OVERRIDE
        _ARCPY_OVERRIDE = stub
        out, err = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = main(argv)
        finally:
            _ARCPY_OVERRIDE = None
        return code, out.getvalue(), err.getvalue()

    edits_text = ("key,field,expected,new\n"
                  "A01,CONDITION,fair,good\n"
                  "A02,CONDITION,fair,good\n"
                  "A03,LANES,2,4\n")
    edits_csv = tmpfile("edits.csv", edits_text)

    raw, header = read_edits(edits_csv)
    check(header[:4] == list(EDIT_COLUMNS), "the edits CSV header reads back")
    check(len(raw) == 3, "three reviewed edits read back")
    bom_csv = tmpfile("bom.csv", edits_text, encoding="utf-8-sig")
    bom_raw, bom_header = read_edits(bom_csv)
    check(bom_header[0] == "key",
          "a UTF-8 BOM is stripped from the first column name  <-- pinned defect")
    check(bom_raw[0]["key"] == "A01",
          "the key column of a BOM file is still addressable by name")

    base = ["--fc", _StubArcpy.FC, "--edits", edits_csv, "--key-field", "ASSETID"]

    stub = _StubArcpy(_stub_table(), STUB_TYPES)
    code, out, err = run_cli(base, stub)
    check(code == 0, "a clean plan checks out at exit 0")
    check("3 edit(s) across 3 row(s)" in out, "the check reports the plan")
    check(stub.updates == 0, "the check writes nothing at all  <-- pinned defect")
    check("Check only" in out, "the check says it wrote nothing")

    stub = _StubArcpy(_stub_table(), STUB_TYPES)
    code, out, err = run_cli(base + ["--apply"], stub)
    check(code == 0, "the same plan applies at exit 0")
    check(stub.updates == 3, "three rows were written")
    check(stub.rows[1]["CONDITION"] == "good", "row 1 holds the new value")
    check(stub.rows[3]["LANES"] == 4,
          "the Integer column was written as an int, not as text")
    check(isinstance(stub.rows[3]["LANES"], int), "and is still an int")
    check("APPLIED -- 3 row(s)" in out, "the apply reports what it wrote")
    check(stub.editor_calls == [], "no edit session is opened without --workspace")

    # ---- the same apply again, which must now be a no-op
    code, out, err = run_cli(base + ["--apply"], stub)
    check(code == 0, "re-running the applied batch exits 0")
    check(stub.updates == 3, "and writes nothing the second time  <-- pinned defect")
    check("already hold the new value" in out, "it says why there is nothing to do")

    # ---- a conflict through the CLI
    table = _stub_table()
    table[1]["CONDITION"] = "somebody else fixed it"      # OBJECTID 2
    stub = _StubArcpy(table, STUB_TYPES)
    code, out, err = run_cli(base + ["--apply"], stub)
    check(code == 1, "a conflict exits 1 even though the other rows landed")
    check("1 CONFLICT(S)" in out, "the conflict is reported")
    check(stub.updates == 2, "the two clean rows still applied")
    check(stub.rows[2]["CONDITION"] == "somebody else fixed it",
          "the drifted row was not overwritten  <-- pinned defect")

    # ---- the edit session, and the drift that aborts it
    ws = ["--workspace", "stub.gdb"]
    stub = _StubArcpy(_stub_table(), STUB_TYPES)
    code, out, err = run_cli(base + ws + ["--apply"], stub)
    check(code == 0, "an apply inside an edit session succeeds")
    check([c[0] for c in stub.editor_calls]
          == ["Editor", "startEditing", "startOperation", "stopOperation",
              "stopEditing(True)"],
          "the edit session opens, operates and commits in that order")

    def drift_row_seven(table):
        table.rows[7]["CONDITION"] = "changed between the two passes"

    drift_edits = tmpfile("drift.csv", "key,field,expected,new\n" + "".join(
        "A%02d,CONDITION,%s,good\n" % (i, "poor" if i == 7 else "fair")
        for i in range(1, 11)))
    drift_base = ["--fc", _StubArcpy.FC, "--edits", drift_edits,
                  "--key-field", "ASSETID"]
    stub = _StubArcpy(_stub_table(), STUB_TYPES, hook=drift_row_seven,
                      hook_at=2)   # cursor 1 builds the plan, cursor 2 pre-flights
    code, out, err = run_cli(drift_base + ws + ["--apply"], stub)
    check(code == 2, "a drift during the apply exits 2, not 1")
    check(stub.updates == 0,
          "row 7 drifting between the plan and the write writes ZERO rows"
          "  <-- pinned defect")
    check("changed under us" in err, "the drift is reported on stderr")
    check("abortOperation" in [c[0] for c in stub.editor_calls],
          "the edit session is aborted, not committed")
    check("stopEditing(False)" in [c[0] for c in stub.editor_calls],
          "and is closed without saving")

    # ---- the snapshot
    snap_path = os.path.join(tmp, "before.json")
    stub = _StubArcpy(_stub_table(), STUB_TYPES)
    code, out, err = run_cli(base + ["--snapshot", snap_path, "--apply"], stub)
    check(code == 0, "an apply with a snapshot succeeds")
    check(os.path.exists(snap_path), "the snapshot file was written")
    with open(snap_path) as handle:
        snap = json.load(handle)
    check(len(snap) == 3, "the snapshot holds one entry per touched row")
    check(snap[0]["CONDITION"] == "fair",
          "the snapshot holds the value from BEFORE the write")
    check("OBJECTID" in snap[0], "the snapshot carries the OBJECTID")
    check("wrote %s" % snap_path in out, "the snapshot path is printed")
    lanes = [entry for entry in snap if entry["OBJECTID"] == "3"]
    check(lanes and lanes[0]["LANES"] == "2",
          "a number in the snapshot is the text of the value, not a JSON number")
    # The apply re-reads only the rows it is about to write. On a class of any
    # size, re-reading all of them inside an open edit session is the
    # difference between a minute and an afternoon.
    check(stub.wheres[0] is None, "the plan is built from a read of every row")
    check(stub.wheres[-1] == "OBJECTID IN (1,2,3)",
          "and the write pass is scoped to the planned OBJECTIDs")
    check(stub.wheres[-2] == "OBJECTID IN (1,2,3)",
          "as is the re-read the write pass checks against")

    # ---- a NULL in the snapshot, which JSON would otherwise render as null
    nulled = _stub_table()
    nulled[0]["CONDITION"] = None                        # OBJECTID 1
    null_csv = tmpfile("nullsnap.csv", "key,field,expected,new\n"
                                       "A01,CONDITION,%s,good\n" % NULL_TOKEN)
    null_snap = os.path.join(tmp, "nullsnap.json")
    stub = _StubArcpy(nulled, STUB_TYPES)
    code, out, err = run_cli(["--fc", _StubArcpy.FC, "--edits", null_csv,
                              "--key-field", "ASSETID",
                              "--snapshot", null_snap, "--apply"], stub)
    check(code == 0 and stub.updates == 1, "a NULL row applies")
    with open(null_snap) as handle:
        null_entry = json.load(handle)[0]
    check(null_entry["CONDITION"] == NULL_TOKEN,
          "a NULL in the snapshot is the null token, so restoring it is "
          "unambiguous  <-- pinned defect")
    stub = _StubArcpy(_stub_table(), STUB_TYPES)
    code, out, err = run_cli(base + ["--apply"], stub)
    check("no rollback snapshot" in err,
          "applying without --snapshot warns that there is no way back")

    # ---- the unique guard through the CLI
    uniq_csv = tmpfile("uniq.csv", "key,field,expected,new\n"
                                   "A01,ASSETID,A01,A09\n")
    stub = _StubArcpy(_stub_table(), STUB_TYPES)
    code, out, err = run_cli(["--fc", _StubArcpy.FC, "--edits", uniq_csv,
                              "--key-field", "ASSETID",
                              "--unique-field", "ASSETID"], stub)
    check(code == 1, "a unique collision exits 1")
    check("already held by OBJECTID [9]" in err,
          "the collision names the OBJECTID holding the value")
    check(stub.updates == 0, "and writes nothing")

    # ---- the usage errors
    check(run_cli(["--edits", edits_csv])[0] == 64, "--fc is required")
    check(run_cli(["--fc", "f"])[0] == 64, "--edits is required")
    # --sample reaches describe, not only the parser
    code, out, err = run_cli(base + ["--sample", "1"],
                             _StubArcpy(_stub_table(), STUB_TYPES))
    check(code == 0 and "...and 2 more" in out,
          "--sample truncates the printed report and says how many it hid")
    check(out.count("CONDITION      'fair' -> 'good'") == 1,
          "and one edit is all it printed")
    check(run_cli(base + ["--sample", "0"])[0] == 64,
          "--sample below 1 is a usage error")
    missing = os.path.join(tmp, "nope.csv")
    check(run_cli(["--fc", _StubArcpy.FC, "--edits", missing],
                  _StubArcpy(_stub_table(), STUB_TYPES))[0] == 2,
          "an unreadable edits CSV exits 2")
    bad_csv = tmpfile("bad.csv", "key,field,expected\nA01,CONDITION,fair\n")
    check(run_cli(["--fc", _StubArcpy.FC, "--edits", bad_csv],
                  _StubArcpy(_stub_table(), STUB_TYPES))[0] == 64,
          "an edits CSV missing a column exits 64")
    date_csv = tmpfile("date.csv", "key,field,expected,new\nA01,BUILT,x,y\n")
    code, out, err = run_cli(["--fc", _StubArcpy.FC, "--edits", date_csv,
                              "--key-field", "ASSETID"],
                             _StubArcpy(_stub_table(), STUB_TYPES))
    check(code == 64 and "Date column" in err,
          "an edit to a Date column exits 64 and says why")

    gone_stub = _StubArcpy(_stub_table(), STUB_TYPES)
    check(run_cli(["--fc", "nosuch.gdb/Assets", "--edits", edits_csv],
                  gone_stub)[0] == 2,
          "a feature class that does not exist exits 2")

    empty_csv = tmpfile("empty.csv", "key,field,expected,new\n")
    code, out, err = run_cli(["--fc", _StubArcpy.FC, "--edits", empty_csv,
                              "--key-field", "ASSETID"],
                             _StubArcpy(_stub_table(), STUB_TYPES))
    check(code == 0 and "Nothing to write" in out,
          "an edits CSV with no edits exits 0 and writes nothing")

    unmatched_csv = tmpfile("unmatched.csv", "key,field,expected,new\n"
                                             "Z99,CONDITION,fair,good\n")
    code, out, err = run_cli(["--fc", _StubArcpy.FC, "--edits", unmatched_csv,
                              "--key-field", "ASSETID"],
                             _StubArcpy(_stub_table(), STUB_TYPES))
    check(code == 1 and "matched no row" in out,
          "a reviewed key that matches no row exits 1")

    null_csv = tmpfile("null.csv", "key,field,expected,new\n"
                                   "A01,OWNER,county,<null>\n")
    stub = _StubArcpy(_stub_table(), STUB_TYPES)
    code, out, err = run_cli(["--fc", _StubArcpy.FC, "--edits", null_csv,
                              "--key-field", "ASSETID", "--apply"], stub)
    check(code == 0 and stub.rows[1]["OWNER"] is None,
          "the null token writes a real NULL  <-- pinned defect")

    # ---- a reviewed value that cannot go into the column it names
    typed_csv = tmpfile("typed.csv", "key,field,expected,new\n"
                                     "A01,LANES,2,two lanes\n")
    code, out, err = run_cli(["--fc", _StubArcpy.FC, "--edits", typed_csv,
                              "--key-field", "ASSETID"],
                             _StubArcpy(_stub_table(), STUB_TYPES))
    check(code == 64 and "line 2" in err,
          "text reviewed into an Integer column is refused, naming the line")

    # ---- the drift path with no edit session at all
    stub = _StubArcpy(_stub_table(), STUB_TYPES, hook=drift_row_seven,
                      hook_at=2)
    code, out, err = run_cli(drift_base + ["--apply"], stub)
    check(code == 2 and stub.updates == 0,
          "a drift with no --workspace still writes zero rows")
    check(stub.editor_calls == [],
          "and no edit session was opened to roll back")

    # ---- a database that refuses the write
    stub = _StubArcpy(_stub_table(), STUB_TYPES)
    stub.fail_write = True
    code, out, err = run_cli(base + ws + ["--apply"], stub)
    check(code == 2 and "rolled back" in err,
          "a write the database refuses exits 2 and says it rolled back")
    check("abortOperation" in [c[0] for c in stub.editor_calls],
          "and the edit session was aborted")
    check(stub.rows[1]["CONDITION"] == "fair",
          "the abort put the row back")

    # ---- a key field the layer does not have
    code, out, err = run_cli(["--fc", _StubArcpy.FC, "--edits", edits_csv,
                              "--key-field", "NOSUCH"],
                             _StubArcpy(_stub_table(), STUB_TYPES))
    check(code == 64 and "key field" in err,
          "a --key-field the layer does not have exits 64")

    # ---- a unique field the layer does not have. Dropping it from the read
    # instead would report a clean apply while enforcing nothing.
    code, out, err = run_cli(base + ["--unique-field", "NOSUCH"],
                             _StubArcpy(_stub_table(), STUB_TYPES))
    check(code == 64 and "NOSUCH" in err,
          "a misspelt --unique-field exits 64 rather than enforcing nothing"
          "  <-- pinned defect")
    code, out, err = run_cli(base + ["--unique-field", "ASSETID",
                                     "--unique-field", "NOSUCH"],
                             _StubArcpy(_stub_table(), STUB_TYPES))
    check(code == 64, "and one bad name spoils a list that is otherwise good")

    # ---- a class that names its rows something other than OBJECTID
    fid_types = dict((k, v) for k, v in STUB_TYPES.items() if k != "OBJECTID")
    fid_types["FID"] = "OID"
    code, out, err = run_cli(base, _StubArcpy(_stub_table(), fid_types))
    check(code == 64 and "OBJECTID" in err,
          "a class with no OBJECTID is refused before any cursor opens")

    # ---- an edits file with nothing in it at all, not even a header
    code, out, err = run_cli(["--fc", _StubArcpy.FC,
                              "--edits", tmpfile("empty.csv", ""),
                              "--key-field", "ASSETID"],
                             _StubArcpy(_stub_table(), STUB_TYPES))
    check(code == 64 and "missing the column" in err,
          "an empty edits file names the columns it wanted")

    # ---- a snapshot that cannot be written
    code, out, err = run_cli(base + ["--snapshot",
                                     os.path.join(tmp, "nodir", "s.json"),
                                     "--apply"],
                             _StubArcpy(_stub_table(), STUB_TYPES))
    check(code == 2 and "could not write the snapshot" in err,
          "an unwritable snapshot exits 2 BEFORE anything is written"
          "  <-- pinned defect")

    # ---- the stub's own read cursor refuses to write, which is what proves
    # the pre-flight pass never touches the layer
    read_only = _StubArcpy(_stub_table(), STUB_TYPES).da.SearchCursor(
        _StubArcpy.FC, ["OBJECTID"])
    raises(lambda: read_only.updateRow([1]),
           "a SearchCursor cannot write, so the pre-flight pass cannot either",
           RuntimeError)

    shutil.rmtree(tmp, ignore_errors=True)

    print("-" * 68)
    total = passed[0] + len(failed)
    if failed:
        print("%d assertions, %d failed" % (total, len(failed)))
        for f in failed:
            print("  FAILED: %s" % f)
        return 1
    print("%d assertions, 0 failed" % total)
    return 0


# ----------------------------------------------------------------------- cli

def read_edits(path):
    """Raw rows and the header of the reviewed CSV."""
    # utf-8-sig, because Excel writes a UTF-8 BOM and a reviewed list is a
    # spreadsheet. Read as plain UTF-8 the first header cell arrives as
    # "\ufeffkey", every key reads as absent, and the whole batch looks empty.
    with open(path, "r", newline="", encoding="utf-8-sig",
              errors="replace") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
        return rows, list(reader.fieldnames or [])


def _parse(argv):
    ap = argparse.ArgumentParser(
        prog="fcpatch.py",
        description="Apply a reviewed list of attribute corrections, and "
                    "refuse every row whose current value is no longer the "
                    "value that was reviewed.",
        epilog="Nothing is written without --apply. The reviewed CSV needs the "
               "columns key, field, expected and new.",
    )
    ap.add_argument("--fc", help="feature class or table to patch")
    ap.add_argument("--edits", help="reviewed CSV of key, field, expected, new")
    ap.add_argument("--key-field", dest="key_field", default=DEFAULT_KEY_FIELD,
                    help="column the CSV keys rows by (default %s)"
                         % DEFAULT_KEY_FIELD)
    ap.add_argument("--unique-field", dest="unique_field", action="append",
                    default=[],
                    help="column whose values must stay unique. Repeatable. An "
                         "edit moving a value onto another row is refused.")
    ap.add_argument("--workspace",
                    help="workspace to open an edit session on. Required for "
                         "versioned data, which a bare cursor cannot commit to.")
    ap.add_argument("--snapshot",
                    help="JSON file to write the pre-edit state of every "
                         "touched row to, before writing anything")
    ap.add_argument("--sample", type=int, default=DEFAULT_SAMPLE,
                    help="conflicts and edits printed before the report "
                         "truncates (default %d)" % DEFAULT_SAMPLE)
    ap.add_argument("--apply", action="store_true",
                    help="write the planned edits. Without this nothing is "
                         "written.")
    ap.add_argument("--self-test", dest="self_test", action="store_true",
                    help="run the offline assertions and exit")
    return ap.parse_args(argv)


def main(argv=None):
    args = _parse(sys.argv[1:] if argv is None else argv)

    if args.self_test:
        return self_test()

    if not args.fc or not args.edits:
        print("error: --fc and --edits are both required. Use --self-test to "
              "verify the tool without a geodatabase.", file=sys.stderr)
        return 64
    if args.sample < 1:
        print("error: --sample must be at least 1.", file=sys.stderr)
        return 64

    try:
        raw_rows, header = read_edits(args.edits)
    except (IOError, OSError, csv.Error) as exc:
        print("error: could not read %s: %s" % (args.edits, exc),
              file=sys.stderr)
        return 2

    try:
        edits = parse_edits(raw_rows, header)
    except ValueError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 64

    arcpy = _import_arcpy()
    if not arcpy.Exists(args.fc):
        print("error: cannot reach %s -- check the path, the connection file "
              "and the VPN." % args.fc, file=sys.stderr)
        return 2

    types = field_types_of(arcpy, args.fc)
    wanted = ["OBJECTID", args.key_field]
    wanted += [e.field for e in edits] + list(args.unique_field)
    fields = sorted(set(wanted))
    try:
        # OBJECTID is how every row is named, from the plan through the
        # re-read to the snapshot. A class without one (a shapefile calls it
        # FID) has to be refused here, not halfway through a cursor.
        if "OBJECTID" not in types:
            raise ValueError("%s has no OBJECTID column, and fcpatch names "
                             "every row by its OBJECTID" % args.fc)
        check_fields(edits, types)
        if args.key_field not in types:
            raise ValueError("the key field %r is not a column of %s"
                             % (args.key_field, args.fc))
        # A misspelt --unique-field would otherwise be dropped from the read
        # and never consulted, so the run would report a clean apply while
        # enforcing nothing. Refusing is the whole job here.
        unknown = [f for f in args.unique_field if f not in types]
        if unknown:
            raise ValueError("the unique field(s) %s are not columns of %s"
                             % (", ".join(sorted(set(unknown))), args.fc))
        rows = read_rows(arcpy, args.fc, fields)
        plan = build_plan(edits, rows, args.key_field, types,
                          tuple(args.unique_field))
    except UniqueCollision as exc:
        # Not a usage error. The reviewed list is well formed and the layer is
        # intact; the two simply cannot both be true, which is the same class
        # of refusal as a conflict and gets the same exit code.
        print("error: %s" % exc, file=sys.stderr)
        return 1
    except ValueError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 64

    for line in describe(plan, args.sample):
        print(line)

    refused = bool(plan.conflicts or plan.unmatched)

    if not plan.changes:
        print("\nNothing was written.")
        return 1 if refused else 0

    if not args.apply:
        print("\nCheck only. %s was not touched. Re-run with --apply to write "
              "%d edit(s)." % (args.fc, len(plan.changes)))
        return 1 if refused else 0

    if args.snapshot:
        try:
            touched = read_rows(arcpy, args.fc, fields,
                                oid_where(set(c.oid for c in plan.changes)))
            write_snapshot(args.snapshot, touched)
        except (IOError, OSError, ValueError) as exc:
            print("error: could not write the snapshot %s: %s"
                  % (args.snapshot, exc), file=sys.stderr)
            return 2
        print("\nwrote %s" % args.snapshot)
    else:
        print("warning: no rollback snapshot was written. Pass --snapshot to "
              "keep one.", file=sys.stderr)

    try:
        written = apply_with_arcpy(arcpy, args.fc, args.workspace, plan.changes)
    except PlanDrifted as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    except Exception as exc:
        print("error: the apply failed and was rolled back: %s" % exc,
              file=sys.stderr)
        return 2

    print("\nAPPLIED -- %d row(s) updated, %d field edit(s)."
          % (written, len(plan.changes)))
    return 1 if refused else 0


if __name__ == "__main__":
    sys.exit(main())
