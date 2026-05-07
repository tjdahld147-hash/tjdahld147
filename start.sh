#!/bin/sh

echo "$PRIVATE_KEY" > /tmp/private-key.pem

openssl req -x509 -nodes -newkey ec -pkeyopt ec_paramgen_curve:secp521r1 \

  -subj '/CN=localhost' -keyout /tmp/tls-key.pem -out /tmp/tls-cert.pem -sha256 -days 3650 2>/dev/null

tesla-http-proxy -tls-key /tmp/tls-key.pem -cert /tmp/tls-cert.pem -port 4443 -key-file /tmp/private-key.pem -host 127.0.0.1 &

sleep 2 && socat TCP-LISTEN:${PORT:-10000},fork,reuseaddr OPENSSL:127.0.0.1:4443,verify=0
