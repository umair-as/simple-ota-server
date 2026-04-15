#!/bin/bash
set -e

# Allow explicit override via environment variable (recommended when host
# has multiple interfaces on the same subnet, e.g. WiFi + ethernet).
if [ -n "$MDNS_IP" ]; then
    IP="$MDNS_IP"
else
    # Fall back to the IP used for the default route.
    IP=$(ip -4 route get 1.1.1.1 | awk '/src/ {for(i=1;i<=NF;i++) if($i=="src") print $(i+1)}')
fi

if [ -z "$IP" ]; then
    echo "ERROR: Could not determine LAN IP"
    exit 1
fi

echo "Publishing ota-gw.local -> $IP via host avahi"
exec avahi-publish -a -R ota-gw.local "$IP"
