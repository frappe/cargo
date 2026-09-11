# A bare Ubuntu host with systemd as PID 1, which is what setup.sh expects: pilot
# deploys the bench as systemd user units and refuses to without a running manager.
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive container=docker

RUN apt-get update && apt-get install -y --no-install-recommends \
		systemd systemd-sysv dbus dbus-user-session \
		curl ca-certificates sudo locales tzdata iproute2 git cron \
	&& rm -rf /var/lib/apt/lists/*

# Pilot schedules backups through the user crontab and does not install cron itself.
# A real Ubuntu server image ships it; this base image does not.

# systemd in a container has no console, no getty and nothing to swap.
RUN systemctl mask \
		systemd-udevd.service systemd-udevd-kernel.socket systemd-udevd-control.socket \
		systemd-modules-load.service sys-kernel-debug.mount sys-kernel-config.mount \
		getty.target console-getty.service dev-hugepages.mount

RUN locale-gen en_US.UTF-8
ENV LANG=en_US.UTF-8

STOPSIGNAL SIGRTMIN+3
CMD ["/lib/systemd/systemd"]
