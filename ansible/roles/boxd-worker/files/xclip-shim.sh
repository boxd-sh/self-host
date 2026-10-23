#!/bin/bash
# boxd xclip shim — intercepts clipboard reads and routes them to the user's
# laptop clipboard via the proxy device endpoint (direct VM→proxy, the proxy
# authenticates by VM source IP). Falls back to real xclip if unavailable.

REAL_XCLIP="/usr/bin/xclip"
FETCH_TIMEOUT="5"

# The proxy device endpoint + device id are injected into the session env when
# the connecting device has client utilities enabled. Without them, there's
# nothing to bridge to — just be a normal xclip.
if [ -z "$BOXD_PROXY_ENDPOINT" ] || [ -z "$BOXD_DEVICE_ID" ]; then
    exec "$REAL_XCLIP" "$@"
fi

BASE="http://${BOXD_PROXY_ENDPOINT}/device/${BOXD_DEVICE_ID}"

# Join all arguments into a single string for pattern matching
ARGS="$*"

# Only intercept clipboard read operations (-o = output)
case "$ARGS" in
    *"-selection clipboard"*"-t TARGETS"*"-o"*|*"-selection clipboard"*"-o"*"-t TARGETS"*)
        RESP=$(curl -sf --max-time "$FETCH_TIMEOUT" "${BASE}/clipboard/type" 2>/dev/null)
        if [ $? -eq 0 ]; then
            TYPE=$(echo "$RESP" | grep -o '"type":"[^"]*"' | cut -d'"' -f4)
            FORMAT=$(echo "$RESP" | grep -o '"format":"[^"]*"' | cut -d'"' -f4)
            case "$TYPE" in
                image)
                    echo "image/${FORMAT:-png}"
                    exit 0
                    ;;
                text)
                    echo "UTF8_STRING"
                    exit 0
                    ;;
            esac
        fi
        # Fallback to real xclip
        exec "$REAL_XCLIP" "$@"
        ;;

    *"-selection clipboard"*"-t image/"*"-o"*|*"-selection clipboard"*"-o"*"-t image/"*)
        TMPFILE=$(mktemp)
        if curl -sf --max-time "$FETCH_TIMEOUT" "${BASE}/clipboard/image" -o "$TMPFILE" 2>/dev/null; then
            cat "$TMPFILE"
            rm -f "$TMPFILE"
            exit 0
        fi
        rm -f "$TMPFILE"
        # Fallback to real xclip
        exec "$REAL_XCLIP" "$@"
        ;;

    *)
        # Everything else — passthrough
        exec "$REAL_XCLIP" "$@"
        ;;
esac
