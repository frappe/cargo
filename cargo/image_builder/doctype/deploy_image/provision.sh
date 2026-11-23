#!/bin/bash
set -euo pipefail

rm -f /etc/nginx/sites-enabled/default
install -d -m 0700 /etc/deploy-agent
install -d -m 0755 /var/lib/deploy-agent
nginx -t
nft --check -f /etc/deploy-agent/metadata-block.nft
systemctl daemon-reload
systemctl enable nginx metadata-block.service deploy-agent.timer

apt-get -qqy autoremove --purge
apt-get -qqy clean
rm -rf /var/lib/apt/lists/*

# The build machine's key would otherwise open every VM. cloud-init adds the user's keys at boot.
rm -f /root/.ssh/authorized_keys /home/*/.ssh/authorized_keys
rm -f /etc/ssh/ssh_host_*
truncate -s 0 /etc/machine-id
rm -f /var/lib/dbus/machine-id
cloud-init clean --logs
find /var/log -type f -exec truncate -s 0 {} +
rm -rf /tmp/* /var/tmp/*
rm -f /root/.bash_history /home/*/.bash_history

sync
