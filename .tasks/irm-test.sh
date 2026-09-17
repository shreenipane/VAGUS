#!/bin/bash -p
# irm-test.sh: jail script behind the static launcher ~/.local/bin/irm-test (Intelligent Resource Manager, SECURITY.md T2).
# Runs tests, the BPF build, or long jobs inside bubblewrap: read-only system, no home directory, no network,
# no D-Bus, clean environment, and only the code directories writable. It lives outside the repository so the
# agent cannot change it. -p makes bash ignore BASH_ENV; binaries use absolute paths so PATH cannot redirect them.
set -euo pipefail
REPO="/home/shreenipane/Heavy Coding/Projects/intelligent-resource-manager"
VENV="/home/shreenipane/.local/share/irm/venv"

args=(--ro-bind / / --dev /dev --proc /proc
      --tmpfs /home --tmpfs /root --tmpfs /tmp --tmpfs /var/tmp --tmpfs /run --tmpfs /mnt --tmpfs /media
      --ro-bind "$REPO" "$REPO" --ro-bind "$VENV" "$VENV")
for d in irm tests bpf; do args+=(--bind "$REPO/$d" "$REPO/$d"); done

mode=test
case "${1:-}" in
  bpf) mode=bpf; shift ;;
  run) mode=run; shift
       for d in data/processed models reports; do
         /usr/bin/mkdir -p "$REPO/$d"; args+=(--bind "$REPO/$d" "$REPO/$d")
       done ;;
esac

args+=(--unshare-all --new-session --die-with-parent --cap-drop ALL --chdir "$REPO" --clearenv
       --setenv HOME /tmp --setenv PATH /usr/bin --setenv LANG C.UTF-8 --setenv PYTHONDONTWRITEBYTECODE 1)

case $mode in
  test) exec /usr/bin/timeout 20m /usr/bin/bwrap "${args[@]}" "$VENV/bin/python" -m pytest -p no:cacheprovider -m "not live" "$@" ;;
  bpf)  exec /usr/bin/timeout 10m /usr/bin/bwrap "${args[@]}" /usr/bin/make -C bpf "$@" ;;
  run)  exec /usr/bin/bwrap "${args[@]}" "$VENV/bin/python" -m irm "$@" ;;
esac
