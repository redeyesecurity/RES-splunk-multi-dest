import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "splunk-app" / "etairos_tee" / "bin"))

from listener import _trim_s2s_trailer, _is_text_byte, _scan_first_non_text


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
