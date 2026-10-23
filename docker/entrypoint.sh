#!/bin/sh
# ssh (ansible, IAP/SSM ProxyCommands) needs a passwd entry for the uid
# ./boxd-install runs us as.
if ! getent passwd "$(id -u)" >/dev/null 2>&1; then
    echo "installer:x:$(id -u):$(id -g)::/home/installer:/bin/sh" >> /etc/passwd
fi
exec "$@"
