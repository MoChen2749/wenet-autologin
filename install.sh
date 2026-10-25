#!/usr/bin/env bash
# Deploy wenet-autologin onto an OpenWrt / iStoreOS router over SSH.
#
# Usage:
#   ROUTER=root@192.168.1.1 ./install.sh
#   ROUTER=root@192.168.1.1 SSH_KEY=~/.ssh/id_ed25519 ./install.sh
#   ROUTER=root@192.168.1.1 ./install.sh --keep-conf   # update code, keep credentials
#
# Environment variables:
#   ROUTER     ssh destination (default: root@192.168.1.1)
#   SSH_KEY    optional private key path
#   SSH_OPTS   extra ssh options
#
# The script copies the daemon and procd service, enables the service, and asks
# for the campus portal username/password (input is not echoed and never leaves
# the machine except inside the ssh stream).  It does not print the password.

set -euo pipefail

ROUTER="${ROUTER:-root@192.168.1.1}"
SSH_KEY="${SSH_KEY:-}"
SSH_OPTS="${SSH_OPTS:--o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new}"
KEEP_CONF=0

for arg in "$@"; do
    case "$arg" in
        --keep-conf) KEEP_CONF=1 ;;
        -h|--help) awk 'NR>1 && /^#/ {sub(/^# ?/, ""); print; next} NR>1 {exit}' "$0"; exit 0 ;;
        *) echo "unknown argument: $arg" >&2; exit 2 ;;
    esac
done

[ -f wenet-autologin.py ] || { echo "run this script from the repository root" >&2; exit 1; }
if [ -n "$SSH_KEY" ]; then
    SSH_OPTS="$SSH_OPTS -i $SSH_KEY"
fi

run() { ssh $SSH_OPTS "$ROUTER" "$@"; }

echo "==> checking router prerequisites"
if ! run 'command -v python3 >/dev/null && command -v openssl >/dev/null'; then
    cat >&2 <<EOF
python3 and/or openssl are missing on the router. Install them first:
  ssh $ROUTER 'opkg update && opkg install python3 openssl-util'
EOF
    exit 1
fi

echo "==> installing /usr/local/bin/wenet-autologin.py"
run 'umask 022; cat > /usr/local/bin/wenet-autologin.py && chmod 755 /usr/local/bin/wenet-autologin.py' \
    < wenet-autologin.py

echo "==> installing /etc/init.d/wenet-autologin"
run 'cat > /etc/init.d/wenet-autologin && chmod 755 /etc/init.d/wenet-autologin' \
    < files/etc/init.d/wenet-autologin

write_conf() {
    printf 'Campus portal username / phone: '
    read -r WENET_PHONE
    [ -n "$WENET_PHONE" ] || { echo "empty username, aborting" >&2; exit 1; }
    printf 'Campus portal password (hidden): '
    read -rs WENET_PASS
    printf '\n'
    [ -n "$WENET_PASS" ] || { echo "empty password, aborting" >&2; exit 1; }
    printf '# managed by install.sh -- chmod 600\nPHONE=%s\nPASS=%s\n' \
        "$WENET_PHONE" "$WENET_PASS" \
        | run 'umask 077; cat > /etc/wenet-autologin.conf && chmod 600 /etc/wenet-autologin.conf'
    unset WENET_PASS
    echo "==> credentials written to /etc/wenet-autologin.conf (0600)"
}

if [ "$KEEP_CONF" = 1 ]; then
    echo "==> keeping existing credentials"
    run 'test -f /etc/wenet-autologin.conf' \
        || echo "warning: no credentials on the router yet, the service will not start" >&2
elif run 'test -f /etc/wenet-autologin.conf'; then
    printf 'Credentials already exist on the router. Overwrite them? [y/N] '
    read -r ANSWER
    case "$ANSWER" in
        y|Y) write_conf ;;
        *) echo "==> keeping existing credentials" ;;
    esac
else
    write_conf
fi

echo "==> enabling and (re)starting the service"
run '/etc/init.d/wenet-autologin enable'
run '/etc/init.d/wenet-autologin restart'
sleep 3

echo "==> status"
run '/etc/init.d/wenet-autologin status || true'
run 'tail -n 5 /var/log/wenet-autologin.log 2>/dev/null || true'

cat <<EOF

Done.  Useful commands:

  ssh $ROUTER '/etc/init.d/wenet-autologin status'
  ssh $ROUTER 'tail -f /var/log/wenet-autologin.log'
  ssh $ROUTER 'python3 /usr/local/bin/wenet-autologin.py --check'

Reboot the router once to confirm the login happens automatically after boot.
EOF
