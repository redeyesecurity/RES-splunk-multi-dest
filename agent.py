#!/usr/bin/env python3
"""
Etairos Log Agent
Listens for Splunk Universal Forwarder S2S connections on port 9997 (or TLS),
decodes the wire protocol, and writes extracted log events to a file.

S2S Protocol Notes:
- UF sends a "signature" (4 bytes: '--CH') then streams key-value pairs
- Each KV block: 4-byte big-endian length + data
- Fields encoded as: 4-byte key-len + key + 4-byte val-len + val
- The "_raw" field contains the actual log line

Usage:
  Plain:  python3 agent.py --port 9997 --output /var/log/splunk-extracted.log
  TLS:    python3 agent.py --port 9997 --output /var/log/splunk-extracted.log \
            --tls --cert server.crt --key server.key [--ca ca.crt]
"""

import socket
import ssl
import struct
import threading
import argparse
import logging
import os
import signal
import sys
from datetime import datetime, timezone

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S"
)
log = logging.getLogger("etairos-log-agent")

# Splunk S2S signature header
S2S_SIGNATURE = b"--CH"
S2S_SIGNATURE_LEN = 128  # full handshake block is 128 bytes

shutdown_event = threading.Event()


def read_exactly(sock, n):
    """Read exactly n bytes from socket."""
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionResetError("Connection closed mid-read")
        buf += chunk
    return buf


def decode_kv_block(data):
    """
    Decode a Splunk S2S key-value block.
    Format: [4-byte key-len][key][4-byte val-len][val] repeated
    Returns dict of fields.
    """
    fields = {}
    offset = 0
    while offset < len(data):
        if offset + 4 > len(data):
            break
        key_len = struct.unpack(">I", data[offset:offset+4])[0]
        offset += 4
        if key_len == 0 or offset + key_len > len(data):
            break
        key = data[offset:offset+key_len].decode("utf-8", errors="replace")
        offset += key_len

        if offset + 4 > len(data):
            break
        val_len = struct.unpack(">I", data[offset:offset+4])[0]
        offset += 4
        val = data[offset:offset+val_len].decode("utf-8", errors="replace")
        offset += val_len

        fields[key] = val

    return fields


def handle_client(conn, addr, output_file, lock):
    """Handle a single UF connection."""
    log.info(f"Connection from {addr[0]}:{addr[1]}")
    try:
        # Read and validate S2S handshake signature
        header = read_exactly(conn, S2S_SIGNATURE_LEN)
        if not header.startswith(S2S_SIGNATURE):
            log.warning(f"{addr}: Invalid S2S signature, got {header[:4]!r} — dropping")
            return
        log.info(f"{addr}: S2S handshake OK")

        events_written = 0
        while not shutdown_event.is_set():
            # Each S2S frame: 4-byte length prefix
            try:
                raw_len_bytes = read_exactly(conn, 4)
            except ConnectionResetError:
                break
            frame_len = struct.unpack(">I", raw_len_bytes)[0]

            if frame_len == 0:
                # Keepalive / heartbeat
                continue
            if frame_len > 10 * 1024 * 1024:  # 10MB sanity cap
                log.warning(f"{addr}: Oversized frame {frame_len} bytes, dropping connection")
                break

            frame_data = read_exactly(conn, frame_len)
            fields = decode_kv_block(frame_data)

            # Extract the raw log line (primary field)
            raw = fields.get("_raw", "")
            if not raw:
                # Fall back to assembling visible fields
                raw = " | ".join(f"{k}={v}" for k, v in fields.items() if not k.startswith("_"))

            # Build output line with metadata
            source = fields.get("source", "unknown")
            host = fields.get("host", addr[0])
            sourcetype = fields.get("sourcetype", "unknown")
            ts = fields.get("_time", "")
            if not ts:
                ts = datetime.now(timezone.utc).isoformat()

            line = f"[{ts}] host={host} source={source} sourcetype={sourcetype} | {raw}\n"

            with lock:
                with open(output_file, "a", encoding="utf-8") as f:
                    f.write(line)

            events_written += 1
            if events_written % 100 == 0:
                log.info(f"{addr}: {events_written} events written")

    except Exception as e:
        log.error(f"{addr}: Error — {e}")
    finally:
        conn.close()
        log.info(f"{addr}: Connection closed ({events_written if 'events_written' in dir() else 0} events)")


def make_ssl_context(cert, key, ca=None):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=cert, keyfile=key)
    if ca:
        ctx.load_verify_locations(cafile=ca)
        ctx.verify_mode = ssl.CERT_REQUIRED
    else:
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def run_server(host, port, output_file, tls=False, cert=None, key=None, ca=None):
    raw_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    raw_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    raw_sock.bind((host, port))
    raw_sock.listen(50)
    raw_sock.settimeout(1.0)

    if tls:
        ssl_ctx = make_ssl_context(cert, key, ca)
        log.info(f"TLS enabled (cert={cert}, key={key}, ca={ca or 'none — no client auth'})")

    mode = "TLS" if tls else "plaintext"
    log.info(f"Etairos Log Agent listening on {host}:{port} [{mode}]")
    log.info(f"Writing events to: {output_file}")

    lock = threading.Lock()

    def shutdown(sig, frame):
        log.info("Shutting down...")
        shutdown_event.set()
        raw_sock.close()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    while not shutdown_event.is_set():
        try:
            conn, addr = raw_sock.accept()
        except socket.timeout:
            continue
        except OSError:
            break

        if tls:
            try:
                conn = ssl_ctx.wrap_socket(conn, server_side=True)
            except ssl.SSLError as e:
                log.warning(f"TLS handshake failed from {addr}: {e}")
                conn.close()
                continue

        t = threading.Thread(target=handle_client, args=(conn, addr, output_file, lock), daemon=True)
        t.start()


def main():
    parser = argparse.ArgumentParser(description="Etairos Log Agent — Splunk S2S receiver")
    parser.add_argument("--host", default="0.0.0.0", help="Bind address (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=9997, help="Listen port (default: 9997)")
    parser.add_argument("--output", default="extracted.log", help="Output log file path")
    parser.add_argument("--tls", action="store_true", help="Enable TLS")
    parser.add_argument("--cert", help="TLS certificate file (PEM)")
    parser.add_argument("--key", help="TLS private key file (PEM)")
    parser.add_argument("--ca", help="CA cert for client auth (optional)")
    args = parser.parse_args()

    if args.tls and (not args.cert or not args.key):
        parser.error("--tls requires --cert and --key")

    # Ensure output dir exists
    os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)

    run_server(args.host, args.port, args.output, args.tls, args.cert, args.key, args.ca)


if __name__ == "__main__":
    main()
