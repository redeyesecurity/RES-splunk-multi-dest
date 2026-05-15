import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "splunk-app" / "etairos_tee" / "bin"))

import listener as listener_module
from listener import _trim_s2s_trailer, _is_text_byte, _scan_first_non_text, TeeListener


def _make_listener():
    return TeeListener({"ack": {"enabled": False}, "alternate_stream": {"enabled": False}},
                       logging.getLogger("test"))


def _build_path_then_event_segment(path: bytes, event_text: bytes) -> bytes:
    """Build a tiny S2S-style buffer with a _path channel + _MetaData:Index
    channel carrying one `\\xa1\\x01` event.

    The parser scans byte-by-byte for the next channel header and aborts
    early when a byte in 1..32 (interpreted as a length) would overrun
    the buffer. Real streams are long enough that this only ever truncates
    cleanly. To replicate that in tests, pad with a second `_path`/
    `_MetaData:Index`/`_raw` triple after the event so the scan can find
    a real next-channel marker."""
    def chan(name: bytes, payload: bytes) -> bytes:
        return bytes([len(name)]) + name + payload
    return (
        chan(b"_path", path)
        + chan(b"_MetaData:Index", b"\xa1\x01" + event_text)
        + chan(b"_raw", b"")
        + chan(b"_path", b"/x")
        + chan(b"_MetaData:Index", b"")
        + chan(b"_raw", b"")
        + b"\x00" * 64
    )


def test_strip_protobuf_style_tail_with_newline_boundary():
    # Captured from UF MinIO parquet: splunkd metrics line + S2S framing
    body = b"05-13-2026 16:11:52.060 -0500 INFO  Metrics ev=0\n\x9e\x02\x86\x04\x12\x03FE\x06resent\x04"
    assert _trim_s2s_trailer(body) == b"05-13-2026 16:11:52.060 -0500 INFO  Metrics ev=0\n"


def test_strip_sh_style_tail_no_newline_boundary():
    # Captured from SH MinIO parquet: audit event whose tail flows directly
    # into framing bytes without a trailing newline.
    body = b'... mode="rw-r--r--", hash=]\x9d\x01\x9c\x04\x00\x00\x00\x99\x07\x80\xff\xff\xff\x06\x03B\x00\x86\x91ZFE\x06resent'
    assert _trim_s2s_trailer(body) == b'... mode="rw-r--r--", hash=]'


def test_strip_single_control_byte():
    body = b"foo\xc1"
    assert _trim_s2s_trailer(body) == b"foo"


def test_no_strip_when_clean():
    body = b"foo bar baz\n"
    assert _trim_s2s_trailer(body) == b"foo bar baz\n"


def test_no_strip_when_no_trailing_garbage():
    body = b"foo bar baz"
    assert _trim_s2s_trailer(body) == b"foo bar baz"


def test_strip_preserves_internal_newlines():
    body = b"line1\nline2\nline3\n\x9e\x02\x86"
    assert _trim_s2s_trailer(body) == b"line1\nline2\nline3\n"


def test_strip_does_not_eat_utf8_two_byte_char():
    body = b"caf\xc3\xa9\n"
    assert _trim_s2s_trailer(body) == b"caf\xc3\xa9\n"


def test_strip_does_not_eat_utf8_three_byte_char():
    # € = \xe2\x82\xac
    body = b"price 5\xe2\x82\xac total\n"
    assert _trim_s2s_trailer(body) == b"price 5\xe2\x82\xac total\n"


def test_strip_does_not_eat_utf8_four_byte_char():
    # 🚀 = \xf0\x9f\x9a\x80
    body = b"rocket \xf0\x9f\x9a\x80 launched"
    assert _trim_s2s_trailer(body) == b"rocket \xf0\x9f\x9a\x80 launched"


def test_strip_rejects_overlong_lead_bytes():
    # \xc0 and \xc1 are invalid UTF-8 lead bytes (overlong); must be trimmed
    body = b"foo\xc0\x80bar"
    assert _trim_s2s_trailer(body) == b"foo"


def test_strip_truncated_utf8_sequence():
    # Lead byte with missing continuation at end of bytes — must trim
    body = b"foo\xe2\x82"
    assert _trim_s2s_trailer(body) == b"foo"


def test_strip_long_protobuf_tail():
    body = b"event text\n" + b"\x9e\x02\x86\x04\x12\x03FE\x06resent\x04" * 10
    assert _trim_s2s_trailer(body) == b"event text\n"


def test_is_text_byte_basics():
    assert _is_text_byte(ord("a"))
    assert _is_text_byte(ord(" "))
    assert _is_text_byte(0x0a)  # \n
    assert _is_text_byte(0x09)  # \t
    assert not _is_text_byte(0x00)
    assert not _is_text_byte(0x1c)
    assert not _is_text_byte(0x9e)
    assert not _is_text_byte(0x7f)  # DEL


def test_scan_first_non_text_clean():
    body = b"clean text\n"
    assert _scan_first_non_text(body) == len(body)


def test_scan_first_non_text_finds_first_garbage():
    body = b"abc\x00def\x01"
    assert _scan_first_non_text(body) == 3


# --- regression tests for #7, #8, #9 ---------------------------------------


def test_ocsf_mapper_default_disabled():
    """#7: by default the mapper is None (raw flat events), but importing
    no longer raises a spurious warning."""
    tl = _make_listener()
    assert tl.mapper is None


def test_ocsf_mapper_opt_in_via_config():
    """#7: when config sets ocsf.enabled=true, the import path works."""
    tl = TeeListener(
        {"ack": {"enabled": False}, "alternate_stream": {"enabled": False},
         "ocsf": {"enabled": True}},
        logging.getLogger("test"),
    )
    assert tl.mapper is not None
    assert callable(tl.mapper)
    out = tl.mapper({"_time": 1.0, "_raw": "x", "sourcetype": "syslog"})
    assert isinstance(out, dict)
    assert "class_uid" in out  # OCSF base record always has class_uid


def test_event_host_uses_handshake_host_not_hardcoded():
    """#8: host must come from the S2S handshake, not 'Mac.hostname'."""
    tl = _make_listener()
    segment = _build_path_then_event_segment(
        path=b"\x05/var/log/syslog",
        event_text=b"05-14-2026 12:00:00 hello world\n",
    )
    _, events = tl._parse_s2s_stream(segment, host="lima-splunk-uf")
    assert events, "should parse at least one event"
    for ev in events:
        assert ev["host"] == "lima-splunk-uf"
        assert ev["host"] != "Mac.hostname"


def test_event_host_falls_back_when_handshake_empty():
    """#8 edge: when the handshake yielded no name, don't crash; default sensibly."""
    tl = _make_listener()
    segment = _build_path_then_event_segment(
        path=b"\x05/var/log/syslog",
        event_text=b"05-14-2026 12:00:00 hello\n",
    )
    _, events = tl._parse_s2s_stream(segment, host="")
    assert events
    assert all(ev["host"] == "unknown" for ev in events)


def test_source_collapses_leading_double_slash():
    """#9: /path/from/_path/channel must not produce //path in events."""
    tl = _make_listener()
    # The _path stream from UF sometimes carries an extra leading byte that
    # leaves us with '//opt/...' after the slice; the fix collapses that
    # to a single '/'.
    segment = _build_path_then_event_segment(
        path=b"X//opt/splunkforwarder/var/log/splunk/metrics.log",
        event_text=b"05-14-2026 12:00:00 metrics event line one\n",
    )
    _, events = tl._parse_s2s_stream(segment, host="h")
    assert events
    for ev in events:
        assert ev["source"].startswith("/opt/"), f"unexpected source={ev['source']!r}"
        assert "//" not in ev["source"], f"leading // not collapsed: {ev['source']!r}"


def test_source_preserves_single_slash_path():
    """#9: don't break already-correct paths."""
    tl = _make_listener()
    segment = _build_path_then_event_segment(
        path=b"/var/log/syslog",
        event_text=b"05-14-2026 12:00:00 ok\n",
    )
    _, events = tl._parse_s2s_stream(segment, host="h")
    assert events
    assert all(ev["source"] == "/var/log/syslog" for ev in events)


# --- #13: real index extracted from _MetaData:Index segment ----------------


def _build_path_then_indexed_event_segment(path: bytes, index_name: bytes,
                                            event_text: bytes) -> bytes:
    """Like `_build_path_then_event_segment` but the `_MetaData:Index`
    payload starts with the 1-byte length-prefixed index name observed
    in real S2S v3 captures (`<len:u8><name><marker:2><event>`).

    Example real payload: `\\x0e_introspection\\xf1\\x02{"datetime":"..."}`"""
    def chan(name: bytes, payload: bytes) -> bytes:
        return bytes([len(name)]) + name + payload
    idx_payload = bytes([len(index_name)]) + index_name + b"\xa1\x01" + event_text
    return (
        chan(b"_path", path)
        + chan(b"_MetaData:Index", idx_payload)
        + chan(b"_raw", b"")
        + chan(b"_path", b"/x")
        + chan(b"_MetaData:Index", b"")
        + chan(b"_raw", b"")
        + b"\x00" * 64
    )


def test_event_index_uses_metadata_segment_name():
    """#13: when `_MetaData:Index` carries `_internal` as its length-
    prefixed name, the emitted event should have `index=_internal`, not
    the default `main`."""
    tl = _make_listener()
    segment = _build_path_then_indexed_event_segment(
        path=b"/opt/splunkforwarder/var/log/splunk/splunkd.log",
        index_name=b"_internal",
        event_text=b"05-14-2026 12:00:00 INFO TailReader read file\n",
    )
    _, events = tl._parse_s2s_stream(segment, host="lima-splunk-uf")
    assert events
    for ev in events:
        assert ev["index"] == "_internal", f"expected _internal, got {ev['index']!r}"


def test_event_index_audit_extracted():
    tl = _make_listener()
    segment = _build_path_then_indexed_event_segment(
        path=b"/var/log/audit/audit.log",
        index_name=b"_audit",
        event_text=b"05-14-2026 12:00:00 audit event\n",
    )
    _, events = tl._parse_s2s_stream(segment, host="h")
    assert events
    assert all(ev["index"] == "_audit" for ev in events)


def test_event_index_falls_back_when_segment_has_no_name():
    """If `_MetaData:Index` payload doesn't start with a valid
    length-prefixed name (just `\\xa1\\x01<event>`), fall back to the
    previous `last_index` (default `main` here)."""
    def chan(name: bytes, payload: bytes) -> bytes:
        return bytes([len(name)]) + name + payload
    tl = _make_listener()
    segment = (
        chan(b"_path", b"/var/log/syslog")
        + chan(b"_MetaData:Index", b"\xa1\x01" + b"05-14-2026 12:00:00 hello\n")
        + chan(b"_raw", b"")
        + chan(b"_path", b"/x")
        + chan(b"_MetaData:Index", b"")
        + chan(b"_raw", b"")
        + b"\x00" * 64
    )
    _, events = tl._parse_s2s_stream(segment, host="h")
    assert events
    assert all(ev["index"] == "main" for ev in events)


def test_event_index_rejects_invalid_name_bytes():
    """A 1-byte length that points to non-ASCII junk must NOT be used as
    the index name; fall through to the default."""
    def chan(name: bytes, payload: bytes) -> bytes:
        return bytes([len(name)]) + name + payload
    tl = _make_listener()
    # length=5 but the next 5 bytes are 0xff 0xfe 0x00 0x01 0x02 — not a valid name
    idx_payload = b"\x05\xff\xfe\x00\x01\x02\xa1\x01hello\n"
    segment = (
        chan(b"_path", b"/var/log/syslog")
        + chan(b"_MetaData:Index", idx_payload)
        + chan(b"_raw", b"")
        + chan(b"_path", b"/x")
        + chan(b"_MetaData:Index", b"")
        + chan(b"_raw", b"")
        + b"\x00" * 64
    )
    _, events = tl._parse_s2s_stream(segment, host="h")
    # Either zero events (printable_ratio filter rejected the binary-prefixed
    # text) or events with index=main (fallback). Either way must NOT have
    # \xff/\xfe leaking into the index field.
    for ev in events:
        assert ev["index"] == "main"
