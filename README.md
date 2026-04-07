# Etairos Log Agent

**Splunk S2S tee proxy with OCSF lakehouse output.**

Sits transparently between your Splunk Universal Forwarders and indexers. Receives the UF data stream, extracts log events, converts them to OCSF format, and writes to your lakehouse (S3/Parquet, local files) — while simultaneously forwarding the original stream to your Splunk indexer unchanged.

```
                                    ┌─────────────────────────────┐
                                    │   Real Splunk Indexer       │
                                    │   (unchanged data)          │
                                    └─────────────────────────────┘
                                              ▲
                                              │ raw S2S forward
                                              │
┌──────────────┐    S2S (9997)    ┌───────────┴───────────────────┐
│  Splunk UF   │ ───────────────► │     etairos-log-agent         │
│  (existing)  │ ◄─────────────── │                               │
└──────────────┘    fake ACKs     │  • Decode S2S wire protocol   │
                                  │  • Map to OCSF v1.1           │
                                  │  • Write Parquet / JSON       │
                                  │  • Forward raw to indexer     │
                                  │  • Send fake ACKs to UF       │
                                  └───────────────────────────────┘
                                              │
                                              ▼
                                    ┌─────────────────────────────┐
                                    │   Lakehouse / S3 / Local    │
                                    │   (OCSF Parquet)            │
                                    └─────────────────────────────┘
```

## Why?

- **Splunk migration**: Test your lakehouse pipeline without touching production Splunk
- **Dual-write during transition**: Send to both Splunk and lakehouse simultaneously
- **No Splunk license required**: The agent is pure Python, no Splunk components
- **OCSF standardization**: Convert proprietary Splunk formats to open OCSF schema
- **Zero UF changes**: Just update `outputs.conf` to point at the agent

## Features

| Feature | Description |
|---------|-------------|
| **S2S Protocol** | Full decode of Splunk's proprietary wire format |
| **TLS Support** | Inbound (from UF) and outbound (to indexer) independently configurable |
| **Fake ACKs** | Responds to `useACK=true` UFs so they don't buffer/retransmit |
| **OCSF Mapping** | Converts 7+ event classes (auth, network, DNS, HTTP, process, file, security) |
| **Lakehouse Output** | S3 Parquet, local Parquet, or local JSON — Hive-partitioned |
| **Fail-open/closed** | Choose behavior when indexer is unreachable |
| **Config file** | YAML-based, all settings in one place |
| **No dependencies** | Core agent runs on Python 3.7+ stdlib only |

## Quick Start

### 1. Clone and configure

```bash
git clone https://github.com/redeyesecurity/etairos-log-agent.git
cd etairos-log-agent
cp config.yaml config.local.yaml
# Edit config.local.yaml with your settings
```

### 2. Point your UF at the agent

On the Splunk Universal Forwarder, edit `$SPLUNK_HOME/etc/system/local/outputs.conf`:

```ini
[tcpout]
defaultGroup = etairos

[tcpout:etairos]
server = <agent-host-ip>:9997
# If using TLS:
# sslCertPath = $SPLUNK_HOME/etc/certs/client.pem
# sslRootCAPath = $SPLUNK_HOME/etc/certs/ca.pem
```

Restart the UF:
```bash
$SPLUNK_HOME/bin/splunk restart
```

### 3. Run the agent

```bash
python3 agent.py --config config.local.yaml
```

That's it. Events flow through the agent to your indexer and lakehouse.

---

## Configuration Reference

All settings live in `config.yaml`. CLI args override config file values.

### Listener (inbound from UF)

```yaml
listener:
  host: "0.0.0.0"       # Bind address
  port: 9997            # S2S port (match UF outputs.conf)
  
  tls:
    enabled: false      # Require TLS from UF
    cert: ""            # Server cert PEM path
    key: ""             # Server key PEM path
    ca: ""              # CA cert for client auth (optional)
    verify_client: false
    ignore_ssl: false   # Skip all verification (testing only)
```

### Output (local file)

```yaml
output:
  file: "/var/log/etairos/events.log"
```

One line per event:
```
[2026-04-07T19:40:00+00:00] host=web01 source=/var/log/auth.log sourcetype=linux_secure | Apr 7 14:38:01 sshd[1234]: Accepted publickey for deploy
```

### Forward (to real Splunk indexer)

```yaml
forward:
  enabled: true
  host: "splunk-indexer.corp"
  port: 9997
  
  tls:
    enabled: false
    cert: ""            # Client cert for mutual TLS
    key: ""
    ca: ""              # CA to verify indexer cert
    ignore_ssl: false
  
  failover:
    mode: "fail-open"   # fail-open = drop if indexer down
                        # fail-closed = buffer until recovery
    queue_max: 100000   # Max frames to buffer
    reconnect_delay: 2  # Seconds between retries
```

### Lakehouse (OCSF output)

```yaml
output:
  lakehouse:
    enabled: true
    destination: "s3"      # s3 | local-parquet | local-json
    
    # Local paths (for local-json / local-parquet)
    path: "/var/log/etairos/lakehouse"
    
    # S3 settings
    s3:
      bucket: "my-security-lakehouse"
      prefix: "etairos/ocsf"
      region: "us-east-1"
      access_key: ""       # Blank = use IAM role
      secret_key: ""
    
    # Batching
    batch_size: 1000       # Flush every N events
    flush_interval: 60     # Or every N seconds
    partition_by: "day"    # day | hour | none
```

**Output structure (Hive-partitioned):**
```
s3://my-security-lakehouse/etairos/ocsf/
  class_uid=4002/year=2026/month=04/day=07/ocsf_20260407_194000_a1b2c3d4.parquet
  class_uid=3005/year=2026/month=04/day=07/ocsf_20260407_194000_e5f6g7h8.parquet
```

### ACK (fake indexer acknowledgments)

```yaml
ack:
  enabled: auto        # auto = detect from UF handshake
                       # true = always ACK
                       # false = never ACK (UF will buffer)
  mode: simple         # simple = 4-byte | extended = 8-byte
  window_size: 1       # ACK every N events
```

**Why fake ACKs?** If your UF has `useACK=true`, it expects the indexer to acknowledge each batch. Without ACKs, the UF will:
- Buffer events locally (fills up disk)
- Retransmit after 120s (duplicate events)
- Log warnings constantly

The agent sends fake ACKs so the UF stays happy.

### Logging

```yaml
logging:
  level: "INFO"        # DEBUG | INFO | WARNING | ERROR
  log_every_n: 500     # Progress log every N events
```

---

## OCSF Mapping

The agent maps Splunk events to [OCSF v1.1](https://schema.ocsf.io) based on `sourcetype`:

| Sourcetype Pattern | OCSF Class | Class UID |
|-------------------|------------|-----------|
| `linux_secure`, `wineventlog:security`, SSH keywords | Authentication | 4002 |
| `cisco:asa`, `pan:traffic`, `netflow`, `stream:tcp` | Network Activity | 3005 |
| `stream:dns`, `cisco:umbrella:dns` | DNS Activity | 3001 |
| `apache:access`, `nginx:access`, `iis`, `stream:http` | HTTP Activity | 3003 |
| `sysmon`, `wineventlog:system` | Process Activity | 6001 |
| `auditd`, `linux:audit` | File System Activity | 1001 |
| `snort`, `suricata`, `crowdstrike` | Security Finding | 2001 |
| Everything else | Unknown | 0 |

**Example output (Authentication event):**
```json
{
  "class_uid": 4002,
  "class_name": "Authentication",
  "activity_id": 1,
  "status_id": 1,
  "status": "Success",
  "time": 1712520681000,
  "user": {"name": "deploy", "type_id": 1},
  "dst_endpoint": {"ip": "10.0.0.50"},
  "auth_protocol": "publickey",
  "metadata": {
    "version": "1.1.0",
    "source": "/var/log/auth.log",
    "sourcetype": "linux_secure"
  },
  "raw_data": "Apr 7 14:38:01 sshd[1234]: Accepted publickey for deploy from 10.0.0.5",
  "observables": [
    {"name": "user.name", "type_id": 4, "value": "deploy"}
  ]
}
```

---

## Deployment

### Systemd service

```bash
# Copy files
sudo mkdir -p /opt/etairos-log-agent
sudo cp agent.py ocsf_mapper.py lakehouse_writer.py ack_handler.py config.yaml /opt/etairos-log-agent/
sudo cp etairos-log-agent.service /etc/systemd/system/

# Create user and directories
sudo useradd -r -s /bin/false etairos
sudo mkdir -p /var/log/etairos
sudo chown etairos:etairos /var/log/etairos

# Edit config
sudo vim /opt/etairos-log-agent/config.yaml

# Start
sudo systemctl daemon-reload
sudo systemctl enable --now etairos-log-agent
sudo journalctl -u etairos-log-agent -f
```

### Docker

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY agent.py ocsf_mapper.py lakehouse_writer.py ack_handler.py config.yaml ./
RUN pip install pyarrow boto3  # Only needed for Parquet/S3
EXPOSE 9997
CMD ["python3", "agent.py", "--config", "config.yaml"]
```

```bash
docker build -t etairos-log-agent .
docker run -d -p 9997:9997 -v /path/to/config.yaml:/app/config.yaml etairos-log-agent
```

### Dependencies

| Feature | Dependencies |
|---------|-------------|
| Core agent + local JSON | None (stdlib only) |
| Local Parquet | `pip install pyarrow` |
| S3 Parquet | `pip install pyarrow boto3` |

---

## TLS Setup

### Generate self-signed certs (testing)

```bash
# CA
openssl genrsa -out ca.key 4096
openssl req -x509 -new -nodes -key ca.key -sha256 -days 3650 -out ca.crt -subj "/CN=EtairosCA"

# Server cert (agent listens with this)
openssl genrsa -out server.key 2048
openssl req -new -key server.key -out server.csr -subj "/CN=log-agent"
openssl x509 -req -in server.csr -CA ca.crt -CAkey ca.key -CAcreateserial -out server.crt -days 365 -sha256

# Client cert (agent connects to indexer with this)
openssl genrsa -out client.key 2048
openssl req -new -key client.key -out client.csr -subj "/CN=etairos-agent"
openssl x509 -req -in client.csr -CA ca.crt -CAkey ca.key -CAcreateserial -out client.crt -days 365 -sha256
```

### Use existing Splunk certs

If your UFs already use TLS to talk to the indexer, copy those certs:

```bash
# From the indexer (what UFs trust)
$SPLUNK_HOME/etc/auth/server.pem  # → agent's server.crt + server.key

# From the UF (if indexer requires client auth)
$SPLUNK_HOME/etc/auth/client.pem  # → agent's forward-cert + forward-key
```

---

## Migration Playbook

### Phase 1: Shadow mode (no risk)

1. Deploy agent on a dedicated host
2. Configure: forward enabled, lakehouse enabled
3. Update one test UF to point at agent
4. Verify: Splunk receives events, lakehouse files appear
5. Compare event counts between Splunk and lakehouse

### Phase 2: Expand

1. Roll out to more UFs (10%, 50%, 100%)
2. Monitor agent logs for errors
3. Validate OCSF schema in lakehouse
4. Build queries/dashboards on lakehouse data

### Phase 3: Cutover

1. Disable forwarding (`forward.enabled: false`)
2. UFs now only write to lakehouse
3. Keep Splunk running for historical queries
4. Decommission Splunk on your timeline

### Rollback

At any point: revert UF `outputs.conf` to point directly at Splunk indexer. Zero data loss — the agent never modifies the stream.

---

## Troubleshooting

### UF not connecting

```bash
# Check agent is listening
netstat -tlnp | grep 9997

# Check UF logs
tail -f $SPLUNK_HOME/var/log/splunk/splunkd.log | grep -i tcpout
```

### TLS handshake failures

```bash
# Test with openssl
openssl s_client -connect <agent-ip>:9997

# Check cert dates
openssl x509 -in server.crt -noout -dates
```

### Events not appearing in lakehouse

```bash
# Check agent logs
journalctl -u etairos-log-agent | grep -i lakehouse

# Verify batch flush
# Events buffer until batch_size OR flush_interval — check config
```

### UF buffering / retransmitting

This means ACKs aren't working. Check:
- `ack.enabled` in config (try `true` instead of `auto`)
- UF's `outputs.conf` has `useACK=true`
- Agent logs show "ACK mode active"

---

## Files

| File | Purpose |
|------|---------|
| `agent.py` | Main S2S listener, forwarder, orchestration |
| `ocsf_mapper.py` | Splunk field → OCSF event conversion |
| `lakehouse_writer.py` | Batched async write to S3/Parquet/JSON |
| `ack_handler.py` | Fake S2S ACK responder |
| `config.yaml` | All configuration |
| `etairos-log-agent.service` | systemd unit file |

---

## License

MIT

---

## Contributing

Issues and PRs welcome. For major changes, open an issue first to discuss.
