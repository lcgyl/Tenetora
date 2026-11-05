# Governance State Migration Notes

The governance trail uses `.tenetora/state/trail/` as its append-only JSONL source.
`governance-trail.json` remains a bounded compatibility view for older readers and
is rebuilt with `tenetora trail rebuild --path .`.

All current readers use the shared trail reader. A valid journal can recover a
damaged legacy view; malformed journal records remain blocking and are reported
explicitly. If the legacy view and journal diverge, readers expose the legacy
content for diagnostics while claim consumers must reverify the changed evidence.

Use `tenetora trail compact --keep-events N` to archive older valid records as
compressed JSONL before rewriting the active journal. Removing the compatibility
view requires an explicit migration decision after dual-read verification; this
release does not delete it.

The DSH adapter uses a compact governance context card by default. It keeps the
protocol and core project entry points while omitting the full filename inventory.
Set `contextMode: full` in the row config when a host-side investigation needs
the complete listing. This changes presentation length only; canonical rules and
workflows remain under `.tenetora/` and the CLI still loads the selected slice.
