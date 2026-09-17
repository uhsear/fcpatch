# fcpatch

Apply a reviewed list of attribute corrections to a feature class, and refuse every row whose
current value is no longer the value that was reviewed.

Sixteen park rows are verified on a Tuesday: wrong addresses, two phone numbers, a name with a
trailing space. The corrections go into a spreadsheet, somebody signs off on Wednesday, and the
script runs on Thursday.

Between Tuesday and Thursday a colleague opened the same layer and fixed three of those rows by
hand. The Thursday script does not know that. It writes the Tuesday values into all sixteen rows,
and the three hand fixes are gone. Editor tracking is switched off on that class, so the overwrite
leaves no stamp, no `last_edited_user` and no date. Nobody finds out until the next audit, and by
then nobody can say which value was ever right.

The defect is not in the edits. Every one of them was correct on Tuesday. The defect is that the
script never asked whether the row still held what the reviewer saw.

```
$ python fcpatch.py --self-test
fcpatch self-test: no arcpy, no database, no network
--------------------------------------------------------------------
PASS  a mismatch against the reviewed value is a conflict  <-- pinned defect
PASS  a conflict on one row does not suppress another row's edit
...
PASS  an edit whose row already holds the new value plans nothing
PASS  an already-applied edit is not a conflict, so a second run is a no-op
PASS  the already-correct test runs before the expected test
...
PASS  a unique value held by another row is refused
PASS  a row already holding its own new value does not collide with itself  <-- pinned defect
PASS  a row owning the value it is being given is planned, not refused  <-- pinned defect
PASS  a straight swap of two unique values is refused, not half done
...
PASS  row 7 of 10 drifting aborts the whole apply
PASS  a drift at row 7 of 10 writes ZERO rows  <-- pinned defect
PASS  the backstop stops at the row that moved, having written six
...
PASS  the text 'nan' is a text value, not a NaN  <-- pinned defect
PASS  a NULL does not match an empty cell  <-- pinned defect
PASS  a Double reading 51.0 matches a reviewed 51  <-- pinned defect
PASS  a NaN never matches the value somebody reviewed  <-- pinned defect
PASS  two NaNs are never the same value  <-- pinned defect
...
PASS  the values are NOT stripped  <-- pinned defect
PASS  two edits to one field of one key raise  <-- pinned defect
PASS  a key matching two rows raises rather than picking one  <-- pinned defect
PASS  a key differing only in whitespace is reported, not matched  <-- pinned defect
...
PASS  the check writes nothing at all  <-- pinned defect
PASS  and writes nothing the second time  <-- pinned defect
PASS  the drifted row was not overwritten  <-- pinned defect
PASS  row 7 drifting between the plan and the write writes ZERO rows  <-- pinned defect
PASS  the edit session is aborted, not committed
...
PASS  a NULL in the snapshot is the null token, so restoring it is unambiguous  <-- pinned defect
PASS  the null token writes a real NULL  <-- pinned defect
PASS  a misspelt --unique-field exits 64 rather than enforcing nothing  <-- pinned defect
PASS  an unwritable snapshot exits 2 BEFORE anything is written  <-- pinned defect
PASS  a SearchCursor cannot write, so the pre-flight pass cannot either
--------------------------------------------------------------------
216 assertions, 0 failed
```

## Requirements

ArcGIS Pro's Python for a real run, because reading and writing a feature class needs `arcpy`:

```
"C:\Program Files\ArcGIS\Pro\bin\Python\envs\arcgispro-py3\python.exe" fcpatch.py --self-test
```

`--self-test` needs none of that. The decision core is Python 3.9 with no `arcpy` import, no
network and no database, so you can check the tool before you have a layer to point it at. `arcpy`
is imported inside the one function that reaches a geodatabase, not at the top of the file.
Standard library only: `argparse`, `collections`, `csv`, `json`, `os`, `sys`, and `contextlib`,
`io`, `shutil` and `tempfile` for the self-test's own fixtures. Nothing to install either way.

```
git clone https://github.com/uhsear/fcpatch.git
python fcpatch.py --self-test
```

## Usage

The reviewed edits are a CSV with four columns. Any other column is carried along unread, so a
reviewer can keep a notes or a ticket column in the same file.

```
key,field,expected,new
A01,CONDITION,fair,good
A02,CONDITION,fair,good
A03,LANES,2,4
A07,CONDITION,fair,good
A12,CONDITION,fair,good
```

`key` identifies the row through `--key-field`. `expected` is what the reviewer saw. `new` is the
correction. Write `<null>` in either value column to mean the database NULL: an empty cell means
the empty string, which in a text column is a different value.

Check first. Nothing is written without `--apply`.

```
$ python fcpatch.py --fc prod.sde/Assets --edits reviewed.csv --key-field ASSETID
1 CONFLICT(S) -- these rows changed since review, and are not touched:
  OBJECTID 7 A07 CONDITION: found 'poor', reviewed 'fair'

1 reviewed key(s) matched no row:
  line 6 key 'A12'

3 edit(s) across 3 row(s):
  [   1] A01                          CONDITION      'fair' -> 'good'
  [   2] A02                          CONDITION      'fair' -> 'good'
  [   3] A03                          LANES          '2' -> '4'

Check only. prod.sde/Assets was not touched. Re-run with --apply to write 3 edit(s).
[exit 1]
```

Then apply. `--workspace` opens an edit session, which versioned data needs. `--snapshot` writes
the pre-edit state of every touched row first.

```
$ python fcpatch.py --fc prod.sde/Assets --edits reviewed.csv --key-field ASSETID \
    --workspace prod.sde --snapshot before.json --apply
1 CONFLICT(S) -- these rows changed since review, and are not touched:
  OBJECTID 7 A07 CONDITION: found 'poor', reviewed 'fair'

1 reviewed key(s) matched no row:
  line 6 key 'A12'

3 edit(s) across 3 row(s):
  [   1] A01                          CONDITION      'fair' -> 'good'
  [   2] A02                          CONDITION      'fair' -> 'good'
  [   3] A03                          LANES          '2' -> '4'

wrote before.json

APPLIED -- 3 row(s) updated, 3 field edit(s).
[exit 1]
```

The exit is 1 because the batch did not fully land: one row had drifted and one key matched
nothing. The three rows that still agreed were written anyway, which is the point. One stale row
does not hold up the other fifteen.

Run the same command again and it reports three edits already holding the new value and writes
nothing. A batch that was interrupted half way is re-run the same way.

| Flag | Default | What it does |
|---|---|---|
| `--fc` | none | Feature class or table to patch. Required. |
| `--edits` | none | Reviewed CSV of `key, field, expected, new`. Required. |
| `--key-field` | `OBJECTID` | Column the CSV keys rows by. |
| `--unique-field` | none | Column whose values must stay unique. Repeatable. |
| `--workspace` | none | Workspace to open an edit session on. Versioned data needs it. |
| `--snapshot` | none | JSON file holding the pre-edit state of every touched row. |
| `--sample` | `20` | Conflicts and edits printed before the report truncates. |
| `--apply` | off | Write the planned edits. Without it nothing is written. |
| `--self-test` | off | Run the offline assertions and exit. |

## What it checks

- **The reviewed value, against the row.** Every `expected` cell is compared with what the row
  holds right now. A row that no longer agrees is reported as a conflict and is not touched. This
  is the whole tool: the reviewed value is the reviewer's evidence that they were looking at this
  row and not an older copy of it.
- **The value that is already correct.** A row that already holds the `new` value produces no edit
  and no conflict. That test runs before the `expected` test, on purpose, and it is what makes a
  second run a no-op and lets a half-finished batch be re-run.
- **The whole plan, before the first write.** Every planned row is read again and checked again
  inside the edit session, before one `updateRow` call is made. A row that drifted between the
  plan and the apply stops the batch with **zero** rows written, not with six of ten written.
- **Uniqueness.** With `--unique-field`, an edit that would move a value onto a row that does not
  own it is refused by name: `FACILITYID 51 is already held by OBJECTID [9]`. A row that already
  carries its own new value does not collide with itself, which is what keeps the tool re-runnable.
- **The key.** A key that matches two rows is refused rather than guessed at. A key that matches
  no row is reported, and if the layer holds a value differing only in whitespace, the report says
  so instead of quietly matching through it.
- **The schema.** Every column an edit names is checked against the feature class before an edit
  session is opened. A column that does not exist, a Date, the OID and the geometry are all
  refused there, rather than as a runtime error inside an open transaction. `--key-field` and
  `--unique-field` are checked the same way. A misspelt `--unique-field` is a usage error, because
  the alternative is a run that reports a clean apply while enforcing nothing.
- **The type.** A reviewed cell is converted to what the column stores. `4` into a `SmallInteger`
  is written as the integer 4, and `four` into it is refused with the line number.

## Why not the Field Calculator or an attribute rule

The Field Calculator applies one expression to a selection, and it is the right tool for
"uppercase this column across 40,000 rows". It has no idea what the value was when a person looked
at it, and it cannot be told to skip a row.

An attribute rule fires on edit and can validate the value being written. It cannot express "only
if this row still reads `fair`", because the reviewed value is not in the database. It is in a
spreadsheet on somebody's desk.

The ArcGIS Pro attribute table is the honest alternative for a short list. Sixteen rows can be
fixed by hand in a few minutes, and a human sees a changed row. This is for the list that is too
long for that and too short to be worth a full ETL, and for the case where the review and the
apply are days apart.

Versioning does not solve it either. A version isolates your edits from other people's until you
reconcile, and the reconcile then asks you to pick a winner. That works when a person is watching.
A scheduled apply has nobody to ask, and the default it picks is the one that loses the other
person's work.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Every reviewed edit either applied or was already applied. |
| `1` | At least one reviewed value no longer matches, or one key matched no row. The edits that still agreed were written if `--apply` was given. |
| `2` | The edits CSV could not be read, the feature class could not be reached, the snapshot could not be written, or the apply failed. Nothing was written in the last two cases. |
| `64` | Usage error: a missing flag, a malformed CSV, a column that cannot be written. |

A unique-field collision exits 1, not 64. The reviewed list is well formed and the layer is
intact; the two simply cannot both be true, which is the same class of refusal as a conflict.

## Limits

- It never decides anything. The reviewed list is the decision, and this tool only refuses to
  apply a decision that has gone stale. Working out the right value is still your job.
- It edits attributes, not geometry. Date, Blob, Raster, GlobalID and the OID are refused by name.
  A Date has no unambiguous text form in a reviewed CSV, and the rest are not attributes.
- The comparison is by value, not by version. A row edited twice since the review, back to the
  value the reviewer saw, reads as unchanged and is written. Nothing in a feature class without
  editor tracking can tell those apart.
- A straight swap of two unique values is refused, because the first half of it would collide.
  Move one of them to a free value, apply, then move it to its final value.
- The whole plan is read into memory, and the pre-flight reads every planned row a second time.
  A few thousand edits are fine. A million-row recalculation is the Field Calculator's job.
- The pre-flight and the write are two cursors, not one atomic read-and-write. Another process can
  still edit a row between them. `--workspace` narrows that window to one edit session and gives
  the abort something to roll back; without it the write-time re-check is the only backstop, and
  it stops at the row that moved rather than before it.
- `--snapshot` is a JSON dump of the rows this tool is about to touch, rendered as text. A NULL is
  written as `<null>`, the same token the reviewed CSV uses, so a JSON `null` never has to be told
  apart from a missing key. It is evidence for a person, not a restore script.
- Keys are compared as text, so a key field holding `51` matches a CSV cell reading `51`. It does
  not match `051`, and it should not.
- `--key-field` is not required to be indexed, and the whole table is read once to build the plan.
  On a large class, index it.
- Nothing here understands related tables, relationship classes or attachments. An edit is one
  field of one row.

## Contributing

Open an issue or pull request on GitHub.

## Author

Built by [Asir Khan](https://www.linkedin.com/in/asir-khan-310317264/).

## License

MIT.

## Related

Other single-file tools in this portfolio that pair with this one:

- [safe-republish](https://github.com/uhsear/safe-republish) - refuses the truncate when the
  replacement is not believable, which is the same refusal one layer up
- [fcload](https://github.com/uhsear/fcload) - loads a dataset into a geodatabase, refusing the
  imports that corrupt silently
- [nullscan](https://github.com/uhsear/arcpy-nullscan) - finds the blank attributes a reviewed
  list is usually written to fix
