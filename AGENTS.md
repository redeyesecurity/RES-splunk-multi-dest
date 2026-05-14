# AGENTS.md — Working on RES-splunk-multi-dest

Cross-tool instructions for any AI agent (Claude Code, Cursor, Aider, Copilot Workspace, etc.) that touches this repo. If your tool reads `CLAUDE.md` or `.cursorrules`, treat this file as the source of truth.

## What this repo is

A Splunk app that tees the Universal Forwarder's S2S output to alternate destinations (S3/MinIO Parquet, local JSON/Parquet, etc.) while still forwarding the original stream to the real indexer. Runs as a scripted input inside the UF and the SH (`splunk-app/etairos_tee/`) or standalone (`standalone/`). See `README.md` for the architecture and config surface.

Companion repos: `RES-splunk-caver` (reads the Parquet this writes), `RES-splunk-bucket-export` (offline path).

## Workflow (non-negotiable)

**Every change ships through a GitHub issue.** No exceptions for "small" fixes discovered mid-debugging.

1. **Open an issue first.** Title is symptom-focused. Body has: symptom, cause, fix summary, verification (test names, lab audit output, captured wire bytes if relevant). One issue per discrete fix — don't bundle.
2. **Commit referencing the issue** with `fixes #N` so it auto-closes on push. If you fixed something before realizing it warranted an issue, file the issue retroactively and close it with a comment pointing at the commit hash.
3. **Push.** Don't sit on commits locally.

The combination of issue + commit is the audit trail. A push without an issue is invisible to the project tracker; an issue without a commit is unresolved noise. Both halves required.

**Examples of fixes that absolutely still need issues:**
- A one-line bug found while debugging something else.
- A test you added because you noticed a gap.
- Reverting a previous fix that broke a dependent path (new issue — don't amend the old one).

## Coding

- Edit existing files in preference to creating new ones.
- No comments that describe WHAT the code does; only comments explaining a non-obvious WHY (an S2S wire-format constraint observed in capture, a Splunk-side quirk, etc).
- No backwards-compat shims, no half-finished helpers, no "future-proof" abstractions.
- Don't add error handling for cases that can't happen. Validate at trust boundaries (the UF's S2S socket, the S3 client) only.
- New config knobs only when there's a concrete reason today, not "in case someone wants…"

## S2S wire-format gotchas (collected from packet captures)

These are the kinds of things that look like bugs but are actually protocol quirks. The fixes are in `splunk-app/etairos_tee/bin/listener.py`; the tests in `tests/test_trim.py` are the regression net.

- **`_raw` trails into framing bytes.** Between two `\xa1\x01` event markers, the segment is `<event text><next event's framing metadata>`. The trailer can start right after the event with no `\n` separator (SH audit events) or after a `\n` (UF splunkd metrics). The trailer contains length-prefixed substrings that look printable (`\x03FE\x06resent`), so naive trailing-byte trimming doesn't help. `_scan_first_non_text` does a forward UTF-8-aware scan and cuts at the first byte that isn't valid text. Don't replace that with a regex.
- **The host name comes from the handshake, not the event.** S2S v3 hello frame has `_serverName[256]` at offset 128. Plumb it into `_parse_s2s_stream(host=...)` and stamp it on emitted events. Don't hardcode `"Mac.hostname"` or read it from `socket.gethostname()`.
- **`_path` channel can carry a leading `//` artifact.** The channel data sometimes has a length-byte prefix before a path that already starts with `/`; the slice can produce `//opt/...`. Collapse `^/+` to a single `/` after the slice.
- **OCSF mapping is opt-in via `ocsf.enabled: true` in config.** Default is flat (Splunk-projection) events because the OCSF dict has nested empty structs that pyarrow can't write to Parquet without a schema overhaul. Don't auto-enable.
- **The marker autodetection in `_parse_s2s_stream` is fragile.** It picks the most common 2-byte marker (`\xa1\x01`, `\xca\x01`, etc.) per segment, with single-byte fallbacks. Don't tighten it to just one marker without verifying captures from a recent UF version.

## Verifying changes

Two levels:

1. **Unit tests** (`pytest`) — fast, run on every change. `python -m pytest`.
2. **End-to-end** — bring up the caver lab (see `RES-splunk-caver/lab/README.md`), point a Splunk UF at the listener, generate events with `logger -t ...`, wait for files to land in MinIO, audit the Parquet's `_raw` column:
   ```python
   import boto3, io, pyarrow.parquet as pq
   s3 = boto3.client("s3", endpoint_url="http://127.0.0.1:9000", ...)
   for k in recent_keys:
       table = pq.read_table(io.BytesIO(s3.get_object(...)['Body'].read()))
       for r in table.to_pylist():
           non_text = sum(1 for c in r['_raw']
                          if (ord(c) < 32 and c not in '\n\r\t')
                          or ord(c) == 0x7f or (0x80 <= ord(c) < 0xa0))
           assert non_text == 0, repr(r['_raw'][-100:])
   ```

When you change anything in the parser, the listener PID changes too — old files in S3 from the previous PID don't reflect your fix. Audit by `'_<new-pid>.parquet'` in the key.

**`list_objects_v2` defaults to a 1000-key page.** Use the paginator (`s3.get_paginator('list_objects_v2').paginate(...)`) or you'll think the bucket is mostly empty when it has 50k+ files.

## Deploying to the lab

The listener runs inside Splunk's process tree. After deploying a new `listener.py`:

1. `cp` the file into the right path on the VM (`/opt/splunkforwarder/etc/apps/etairos_tee/bin/listener.py` for UF VM; `/opt/splunk/etc/apps/etairos_tee/bin/listener.py` for SH VM).
2. Clear pyc: `rm -f .../bin/__pycache__/listener.cpython-*.pyc`
3. Kill the running listener (`pkill -9 -f start_listener.py`). Splunk's scripted input has `interval = -1`; it only respawns on splunkd start, so manually relaunch:
   - UF VM: `nohup /opt/splunkforwarder/bin/splunk cmd python3 .../start_listener.py 2>&1 &`
   - SH VM: `env SPLUNK_HOME=/opt/splunk nohup /usr/bin/python3 .../start_listener.py 2>&1 &` (system Python 3.10 because Splunk's bundled 3.9 chokes on `str | None`)

## What NOT to do

- Don't push directly to `main` from an editor's auto-sync — write deliberate commits referencing issues.
- Don't bypass `_trim_s2s_trailer` because "the data looks clean today." UF version drift will reintroduce framing leaks.
- Don't silently catch and swallow exceptions in the S3 write path; they were already biting us with bad ACK responses.
- Don't introduce a new dependency without a reason. The app runs inside Splunk's bundled Python (3.9 on the indexer-class binaries) — anything you import must work there too.
