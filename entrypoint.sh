#!/bin/sh
# Runs as root at startup so it can fix ownership on the /index volume,
# then drops to the non-root nasearch user via gosu.
mkdir -p /index
chown -R nasearch:users /index
exec gosu nasearch uvicorn main:app --host 0.0.0.0 --port 8000
