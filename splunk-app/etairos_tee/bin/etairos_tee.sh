#!/bin/sh
exec /Applications/SplunkForwarder/bin/splunk cmd python3 \
  /Applications/SplunkForwarder/etc/apps/etairos_tee/bin/start_listener.py
