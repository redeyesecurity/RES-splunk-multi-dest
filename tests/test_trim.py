import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "splunk-app" / "etairos_tee" / "bin"))

from listener import _trim_s2s_trailer, _is_text_byte


def test_strip_protobuf_style_tail():
    # Captured from MinIO parquet: real splunkd metrics line + S2S framing leak
    body = b"05-13-2026 16:11:52.060 -0500 INFO  Metrics ev=0\n\x9e\x02\x86\x04\x12\x03FE\x06resent\x04"
    trimmed = _trim_s2s_trailer(body)
    assert trimmed == b"05-13-2026 16:11:52.060 -0500 INFO  Metrics ev=0\n"


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


def test_strip_does_not_eat_utf8_lead_byte():
    # \xc3 is a valid UTF-8 lead byte for é (\xc3\xa9)
    body = b"caf\xc3\xa9\n"
    assert _trim_s2s_trailer(body) == b"caf\xc3\xa9\n"


def test_is_text_byte_basics():
    assert _is_text_byte(ord("a"))
    assert _is_text_byte(ord(" "))
    assert _is_text_byte(0x0a)  # \n
    assert not _is_text_byte(0x00)
    assert not _is_text_byte(0x1c)
    assert not _is_text_byte(0x9e)
    assert not _is_text_byte(0x7f)  # DEL


def test_strip_long_protobuf_tail():
    body = b"event text\n" + b"\x9e\x02\x86\x04\x12\x03FE\x06resent\x04" * 10
    assert _trim_s2s_trailer(body) == b"event text\n"
