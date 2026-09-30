#!/usr/bin/env bash
# The smaller probes, one detached invocation: server + Chrome + probes, then
# tear both down. Same contract as _run_drive.sh: refuse a busy port, prove the
# live server is the code under test, write output inside the project.
set -u
cd "C:/Users/kunal/OneDrive/Desktop/impact/impact_studio" || exit 1
unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy
RUN="tools/ui_probe/_run"; mkdir -p "$RUN" "$RUN/shots"
CHROME="/c/Program Files/Google/Chrome/Application/chrome.exe"
NODE="C:/Users/kunal/.workbuddy-ai/binaries/node/versions/22.22.2-3/node.exe"
PY="C:/Program Files/Python312/python.exe"
PORT="${PORT:-8777}"
if netstat -ano -p TCP 2>/dev/null | grep LISTENING | grep -q ":$PORT "; then
  echo "PORT $PORT BUSY - refusing to start (would drive a stale server)"
  echo "If it is your own 'python run.py', re-run with:  PORT=8787 bash $0"
  exit 1
fi
"$PY" run.py --no-browser --port $PORT > "$RUN/server.log" 2>&1 &
SRV=$!
UP=0
for i in $(seq 1 30); do
  curl -s -m 2 --noproxy '*' "http://127.0.0.1:$PORT/api/memory" >/dev/null 2>&1 && { UP=1; echo "server UP after ${i}s"; break; }
  sleep 1
done
[ "$UP" = "1" ] || { echo "SERVER DID NOT START"; tail -15 "$RUN/server.log"; kill $SRV 2>/dev/null; exit 1; }
WB=$(curl -s -m 8 --noproxy '*' -o /dev/null -w '%{http_code}' \
  "http://127.0.0.1:$PORT/__workbook/TW%20Impact%20Study_V2%201%20(1).xlsx")
[ "$WB" = "200" ] || { echo "WORKBOOK ROUTE $WB - stale server or missing file"; kill $SRV 2>/dev/null; exit 1; }
echo "server serves /__workbook/ (200)"
CDP_PORT="${CDP_PORT:-9222}"
if netstat -ano -p TCP 2>/dev/null | grep LISTENING | grep -q ":$CDP_PORT "; then
  echo "DEBUG PORT $CDP_PORT BUSY - refusing to start (would attach to a foreign browser)"
  echo "Re-run with:  CDP_PORT=9333 PORT=8787 bash $0"
  exit 1
fi
"$CHROME" --headless=new --disable-gpu --remote-debugging-port=$CDP_PORT \
  --user-data-dir="C:/Users/kunal/OneDrive/Desktop/impact/impact_studio/tools/ui_probe/_run/chrome-profile" \
  --no-first-run --no-default-browser-check --hide-scrollbars about:blank \
  > "$RUN/chrome.log" 2>&1 &
CHR=$!
for i in $(seq 1 30); do
  curl -s -m 2 --noproxy '*' http://127.0.0.1:$CDP_PORT/json/version >/dev/null 2>&1 && { echo "chrome UP after ${i}s"; break; }
  sleep 1
done
#    The probes default to 127.0.0.1:8777 when APP_URL is unset, so with the
#    user's own `python run.py` up they would drive THAT instance instead of the
#    one started here. Name the target explicitly.
export APP_URL="http://127.0.0.1:$PORT"
export CDP_URL="http://127.0.0.1:$CDP_PORT"
echo "driving APP_URL=$APP_URL via CDP_URL=$CDP_URL"
cd tools/ui_probe || exit 1
for P in step1_probe profile_probe market_probe composite_probe step4_probe; do
  echo "===== $P ====="
  "$NODE" $P.mjs "_run/shots" > "_run/$P.out" 2>&1
  echo "$P EXIT=$?"
  grep -cE "\[FAIL\]|FAIL " "_run/$P.out" 2>/dev/null | sed 's/^/  FAIL lines: /'
  tail -3 "_run/$P.out"
done
cd "C:/Users/kunal/OneDrive/Desktop/impact/impact_studio"
kill $CHR 2>/dev/null; kill $SRV 2>/dev/null; sleep 1
echo "OTHERS DONE"
