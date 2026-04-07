# etairos-log-agent

Listens for Splunk Universal Forwarder (UF) connections using the S2S wire protocol, extracts raw log events, and writes them to a file.

## How it works

Splunk UF speaks S2S (Splunk-to-Splunk) protocol on port 9997. This agent:
1. Accepts TCP connections from one or more UFs
2. Validates the S2S handshake signature
3. Decodes each frame's key-value fields
4. Extracts `_raw` (the actual log line) plus metadata (`host`, `source`, `sourcetype`, `_time`)
5. Writes one line per event to an output file

## Requirements

- Python 3.7+
- No external dependencies (stdlib only)

## Usage

### Unencrypted (testing)

Point your UF at this host on port 9997 — no `[tcpout-ssl]` stanza needed.

```bash
python3 agent.py --port 9997 --output /var/log/extracted.log
```

### TLS (production)

UF must be configured with matching certs. Agent needs server cert + key; optionally a CA cert to require client auth.

```bash
python3 agent.py --port 9997 --output /var/log/extracted.log \
  --tls --cert server.crt --key server.key --ca ca.crt
```

## Configure Splunk UF to point here

In `outputs.conf` on the UF:

```ini
[tcpout]
defaultGroup = etairos-receiver

[tcpout:etairos-receiver]
server = <this-host-ip>:9997

# For TLS, add:
# [tcpout-ssl]
# sslCertPath = $SPLUNK_HOME/etc/certs/client.pem
# sslRootCAPath = $SPLUNK_HOME/etc/certs/ca.pem
# sslVerifyServerCert = true
```

## Output format

Each line:
```
[2026-04-07T19:40:00+00:00] host=webserver01 source=/var/log/syslog sourcetype=syslog | Apr  7 14:38:01 webserver01 sshd[1234]: Accepted publickey for user
```

## TLS cert generation (self-signed, for testing)

```bash
# Generate CA
openssl genrsa -out ca.key 4096
openssl req -x509 -new -nodes -key ca.key -sha256 -days 3650 -out ca.crt -subj "/CN=EtairosCA"

# Generate server cert
openssl genrsa -out server.key 2048
openssl req -new -key server.key -out server.csr -subj "/CN=log-agent"
openssl x509 -req -in server.csr -CA ca.crt -CAkey ca.key -CAcreateserial -out server.crt -days 365 -sha256
```
