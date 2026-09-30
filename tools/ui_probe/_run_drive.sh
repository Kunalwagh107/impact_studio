#!/usr/bin/env bash
# Detached full-driver run: server + Chrome + probe in ONE invocation, because
# headless Chrome dies when the launching shell returns. Output lands inside the
# project (_run/), never /tmp. A proxy env var makes localhost look broken.
set -u
cd "C:/Users/kunal/OneDrive/Desktop/impact/impact_studio" || exit 1
unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy

RUN="tools/ui_probe/_run"
mkdir -p "$RUN" "$RUN/shots"

CHROME="/c/Program Files/Google/Chrome/Application/chrome.exe"
NODE="C:/Users/kunal/.workbuddy-ai/binaries/node/versions/22.22.2-3/node.exe"

# The interpreter that actually has the project's deps. `python` on PATH resolves
# to the managed runtime, which has none - it starts, prints the banner, then dies
# on `import uvicorn`, and the health loop below only sees "down" for 30s.
PY="C:/Program Files/Python312/python.exe"

# 0. Refuse to start on a busy port. run.py's own fallback ("Starting on port
#    8778 instead") is the dangerous case: the health probe below then talks to
#    the OLD server still holding 8777, which is running pre-edit code. That
#    produced a run where every check failed and the served route was 404 -
#    reading as a broken app rather than a stale one.
#
#    `PORT=8787 bash _run_drive.sh` picks another port. Needed whenever the user
#    has their own `python run.py` up: that instance is running whatever code it
#    started with, so driving it would test the wrong build - and killing it
#    would close the app they are looking at.
PORT="${PORT:-8777}"
if netstat -ano -p TCP 2>/dev/null | grep LISTENING | grep -q ":$PORT "; then
  echo "PORT $PORT IS ALREADY IN USE - refusing to start."
  echo "A previous run's server is still up; run.py would silently fall back to"
  echo "a second port and this script would then drive the STALE server."
  echo "If it is your own 'python run.py', restart it (to pick up backend edits)"
  echo "and re-run with:  PORT=8787 bash $0"
  netstat -ano -p TCP 2>/dev/null | grep LISTENING | grep ":$PORT "
  echo "Kill it (taskkill //F //PID <pid>) and re-run."
  exit 1
fi

# 1. server, on an idle port (nothing else must own the single worker)
"$PY" run.py --no-browser --port $PORT \
  > "$RUN/server.log" 2>&1 &
SRV=$!
SERVER_UP=0
for i in $(seq 1 30); do
  if curl -s -m 2 --noproxy '*' http://127.0.0.1:$PORT/api/memory >/dev/null 2>&1; then
    echo "server UP after ${i}s"; SERVER_UP=1; break
  fi
  sleep 1
done
if [ "$SERVER_UP" = "0" ]; then
  echo "SERVER FAILED TO START - aborting before Chrome, so a dead server"
  echo "cannot be mistaken for a probe failure. Last lines of server.log:"
  tail -15 "$RUN/server.log"
  kill $SRV 2>/dev/null
  exit 1
fi
# Prove the server is running the code under test, not a stale copy. A route the
# current source defines must answer; a 404 here means we are on an old process.
if ! curl -s -m 5 --noproxy '*' -o /dev/null \
     "http://127.0.0.1:$PORT/__workbook/__probe_missing__.xlsx"; then
  echo "SERVER NOT RESPONDING TO /__workbook/ - it is not the code under test."
  exit 1
fi
WB_CODE=$(curl -s -m 8 --noproxy '*' -o /dev/null -w '%{http_code}' \
  "http://127.0.0.1:$PORT/__workbook/TW%20Impact%20Study_V2%201%20(1).xlsx")
if [ "$WB_CODE" != "200" ]; then
  echo "LIVE SERVER RETURNED $WB_CODE FOR THE WORKBOOK ROUTE - expected 200."
  echo "This is a stale server (pre-edit code) or a missing workbook. Aborting."
  exit 1
fi
echo "server serves /__workbook/ (200) - running the code under test"
# Prove the API answers, or every route below reports a phantom failure.
curl -s -m 3 --noproxy '*' http://127.0.0.1:$PORT/api/memory > "$RUN/api_health.json" 2>&1 \
  || echo "API NOT ANSWERING - failures below are not findings" 

# 2. chrome
#    The debug port must also be ours. If something already holds it, this
#    Chrome fails to bind and the probe attaches to *that* browser instead.
CDP_PORT="${CDP_PORT:-9222}"
if netstat -ano -p TCP 2>/dev/null | grep LISTENING | grep -q ":$CDP_PORT "; then
  echo "DEBUG PORT $CDP_PORT IS ALREADY IN USE - refusing to start."
  echo "Re-run with:  CDP_PORT=9333 PORT=8787 bash $0"
  exit 1
fi
rm -rf "$RUN/chrome-profile"
"$CHROME" --headless=new --disable-gpu --remote-debugging-port=$CDP_PORT \
  --user-data-dir="C:/Users/kunal/OneDrive/Desktop/impact/impact_studio/tools/ui_probe/_run/chrome-profile" \
  --no-first-run --no-default-browser-check --hide-scrollbars about:blank \
  > "$RUN/chrome.log" 2>&1 &
CHR=$!
for i in $(seq 1 30); do
  if curl -s -m 2 --noproxy '*' http://127.0.0.1:$CDP_PORT/json/version >/dev/null 2>&1; then
    echo "chrome UP after ${i}s"; break
  fi
  sleep 1
done

# 3. the full seven-step driver.
#    The probe defaults to 127.0.0.1:8777 when APP_URL is unset. With the user's
#    own `python run.py` up, that silently drove THEIR instance - running
#    pre-edit code - while this script's server sat idle on $PORT. It is the same
#    stale-server trap the port guard above exists to stop, arriving by another
#    route, and it reads as a product bug: the probe reported an old message from
#    a build that was never the one under test. Name the target explicitly.
export APP_URL="http://127.0.0.1:$PORT"
export CDP_URL="http://127.0.0.1:$CDP_PORT"
echo "driving APP_URL=$APP_URL via CDP_URL=$CDP_URL"
cd tools/ui_probe || exit 1
"$NODE" drive.mjs "_run/shots" > "_run/drive.out" 2>&1
echo "DRIVE EXIT=$?"
cd "C:/Users/kunal/OneDrive/Desktop/impact/impact_studio"

# 4. tear down both, so nothing is left holding the worker
kill $CHR 2>/dev/null
kill $SRV 2>/dev/null
sleep 1
echo "DONE"
