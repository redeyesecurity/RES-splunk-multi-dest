"""
ack_handler.py — Fake S2S indexer ACK responder

When a Splunk UF has useACK=true in outputs.conf, it expects the indexer
to send back ACK packets for each event batch. Without them, the UF:
  - Buffers data locally (fills $SPLUNK_HOME/var/lib/splunk/fishbucket)
  - Starts retransmitting after ackTimeoutOnForwarder (default 120s)
  - May generate splunkd.log warnings: "Received ack request but ack is not configured"

S2S ACK wire format (observed via Wireshark / Confluent S2S connector source):
  The UF sends an ACK request embedded in the event stream. The indexer
  responds with a simple 4-byte acknowledgment.

  UF -> Indexer (per batch, after the KV block):
    [4 bytes] ACK sequence number (big-endian uint32)

  Indexer -> UF (ACK response):
    [4 bytes] ACK sequence number echo (big-endian uint32)
    -- some implementations also send an 8-byte response:
    [4 bytes] sequence number
    [4 bytes] status (0 = success)

  The S2S capabilities handshake (in the 128-byte header) signals ACK support:
    _s2s_capabilities field contains "ack=1" when the indexer supports ACKs.
    The agent detects this in the handshake and enables ACK mode automatically.

Configuration (config.yaml):
  ack:
    enabled: auto       # auto | true | false
                        #   auto  = detect from UF handshake (recommended)
                        #   true  = always send ACKs (even if UF doesn't request)
                        #   false = never send ACKs (UF will buffer/retransmit)
    mode: simple        # simple = 4-byte echo (most UF versions)
                        # extended = 8-byte (seq + status=0)
    window_size: 1      # ACK every N events (1 = per-event, higher = batched)
                        # Higher values reduce ACK traffic but increase UF buffer use
                        # Recommended: 1 for real-time, 10-100 for bulk migration
"""

import struct
import logging
import threading

log = logging.getLogger("etairos-log-agent.ack")

# Capability string patterns in S2S handshake that indicate UF wants ACKs
ACK_CAPABILITY_PATTERNS = (b"ack=1", b"useACK=true", b"ack=true")


def handshake_requests_ack(header_bytes: bytes) -> bool:
    """
    Scan the 128-byte S2S handshake for ACK capability flags.
    Returns True if the UF is requesting ACK mode.
    """
    for pattern in ACK_CAPABILITY_PATTERNS:
        if pattern in header_bytes:
            log.debug(f"ACK requested by UF (found '{pattern.decode()}' in handshake)")
            return True
    return False


class AckHandler:
    """
    Tracks sequence numbers per connection and sends fake ACK responses
    back to the UF on the same socket.

    Thread-safe: one AckHandler per client connection.
    """

    def __init__(self, sock, mode: str = "simple", window_size: int = 1):
        self._sock       = sock
        self._mode       = mode          # "simple" or "extended"
        self._window     = window_size
        self._seq        = 0             # current sequence counter
        self._pending    = 0             # events since last ACK
        self._lock       = threading.Lock()
        self._enabled    = True

    def disable(self):
        """Turn off ACKing for this connection (e.g., config says false)."""
        self._enabled = False

    def record_event(self):
        """
        Call once per received event/frame. Sends an ACK when the window is full.
        Returns True if an ACK was sent.
        """
        if not self._enabled:
            return False
        with self._lock:
            self._pending += 1
            if self._pending >= self._window:
                self._seq += self._pending
                self._pending = 0
                return self._send_ack(self._seq)
        return False

    def flush(self):
        """
        Send a final ACK for any remaining pending events.
        Call on connection close.
        """
        if not self._enabled:
            return
        with self._lock:
            if self._pending > 0:
                self._seq += self._pending
                self._pending = 0
                self._send_ack(self._seq)

    def _send_ack(self, seq: int) -> bool:
        """
        Send ACK packet to UF.

        simple mode:   4 bytes — big-endian uint32 sequence number
        extended mode: 8 bytes — sequence number + 4-byte status (0 = OK)

        Both modes are accepted by standard Splunk UF versions (6.x+).
        Use extended if you see UF log warnings about malformed ACKs.
        """
        try:
            if self._mode == "extended":
                pkt = struct.pack(">II", seq, 0)   # seq + status=0 (success)
            else:
                pkt = struct.pack(">I", seq)        # simple 4-byte echo

            self._sock.sendall(pkt)
            log.debug(f"ACK sent: seq={seq} mode={self._mode}")
            return True
        except Exception as e:
            log.warning(f"ACK send failed (seq={seq}): {e}")
            self._enabled = False   # stop trying on this conn
            return False


class AckConfig:
    """Parsed ACK configuration from config.yaml ack: block."""

    def __init__(self, raw: dict):
        self.mode_str   = str(raw.get("enabled",     "auto")).lower()
        self.ack_mode   = str(raw.get("mode",        "simple")).lower()
        self.window     = int(raw.get("window_size", 1))

    def should_ack(self, handshake_bytes: bytes) -> bool:
        """
        Decide whether to enable ACKing for this connection.
          auto  -> check handshake
          true  -> always
          false -> never
        """
        if self.mode_str == "false":
            return False
        if self.mode_str == "true":
            return True
        # auto
        return handshake_requests_ack(handshake_bytes)

    def make_handler(self, sock) -> AckHandler:
        return AckHandler(sock, mode=self.ack_mode, window_size=self.window)
