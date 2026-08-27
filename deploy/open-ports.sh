#!/usr/bin/env bash
#
# Open 80/443 in the instance's host firewall — the Oracle trap, handled.
#
# Opening 80/443 in the OCI Security List is NOT sufficient. The stock Ubuntu
# image drops those ports in host iptables independently, and the load-balancer
# health check then fails while every cloud-side setting reads correct. Both
# layers must be open. This script is the host layer; the Security List is a
# console change and is not scripted here.
#
# The chain on this instance ends in:
#     -A INPUT -j REJECT --reject-with icmp-host-prohibited
# with only port 22 accepted above it. New rules must therefore be INSERTED
# above that REJECT. Appending puts them after it, where they never match — the
# single most common way this step looks done and is not.
#
# SAFETY — SSH is the only way into this host. It has no public IP and no
# console access from the dev machine. This script therefore:
#   * never flushes the chain, never sets a policy, never uses -F/-X/-P;
#   * refuses to run at all unless the port-22 ACCEPT is present;
#   * re-asserts that invariant after the edit;
#   * does NOT persist. Persisting is a separate, deliberate second step, taken
#     only after a SECOND SSH SESSION has been confirmed to still connect.
#
# Usage, on the instance:
#     sudo bash deploy/open-ports.sh
#
# Then, from the dev machine, open a second SSH session and confirm it connects.
# Only once that succeeds:
#     sudo netfilter-persistent save     # writes /etc/iptables/rules.v4
#
set -uo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "ABORT: run me as root (sudo bash deploy/open-ports.sh)" >&2
  exit 1
fi

SSH_RULE=(-p tcp -m state --state NEW -m tcp --dport 22 -j ACCEPT)

echo "=== INPUT chain BEFORE ==="
iptables -S INPUT

# Invariant 1: do not touch a chain that is not the one we think it is.
if ! iptables -C INPUT "${SSH_RULE[@]}" 2>/dev/null; then
  echo "ABORT: the port-22 ACCEPT rule is not present. Refusing to modify INPUT." >&2
  exit 1
fi

for port in 80 443; do
  if iptables -C INPUT -p tcp -m state --state NEW -m tcp --dport "$port" -j ACCEPT 2>/dev/null; then
    echo "port ${port}: already accepted, nothing to do"
    continue
  fi

  # Locate the REJECT and insert immediately above it. Recomputed each pass:
  # the previous insert shifted its line number.
  line=$(iptables -L INPUT --line-numbers -n | awk '$2 == "REJECT" { print $1; exit }')
  if [ -z "${line}" ]; then
    echo "ABORT: no REJECT rule found in INPUT — chain is not in the expected shape." >&2
    exit 1
  fi

  iptables -I INPUT "${line}" -p tcp -m state --state NEW -m tcp --dport "$port" -j ACCEPT
  echo "port ${port}: ACCEPT inserted at position ${line}, above the REJECT"
done

echo
echo "=== INPUT chain AFTER ==="
iptables -S INPUT

# Invariant 2: the way back in still exists.
if ! iptables -C INPUT "${SSH_RULE[@]}" 2>/dev/null; then
  echo "ABORT: port-22 ACCEPT vanished during the edit. DO NOT persist. Fix now." >&2
  exit 1
fi

cat <<'NEXT'

OK: port 22 is still accepted, and 80/443 are now accepted above the REJECT.

NOT YET PERSISTED — this is on purpose.
  1. From the dev machine, open a SECOND ssh session:  ssh oci-docker
  2. Only once that connects, persist on the instance:  sudo netfilter-persistent save

If step 1 fails, do not persist: reboot the instance from the OCI console and
the unsaved rules are discarded.
NEXT
