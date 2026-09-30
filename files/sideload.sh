#!/usr/bin/env bash
# Side-load container images into the air-gapped cluster.
#
# The cluster has no route to any registry and Harbor is empty, so every image
# must be present in each node's containerd by hand. Nothing else enforces
# this, and the scheduler does not consider it, so a pod that lands on a node
# without its image sits in ImagePullBackOff indefinitely. This script is that
# missing enforcement.
#
#   ./files/sideload.sh --list
#       Report, per node, which images are missing.
#
#   ./files/sideload.sh <image> [<image>...]
#       Pull each image for linux/arm64 on this host, push it to the in-cluster
#       registry, and import it into every node's containerd.
#
#   ./files/sideload.sh --node pi3 <image>...
#       Import into one node only.
#
# Why --platform is mandatory
# ---------------------------
# This host is x86_64. `docker pull <image>` fetches the amd64 variant, and
# `docker save` then writes an amd64 archive that no arm64 node can run. The
# Longhorn instance-manager image was lost this way for days. Every pull below
# specifies linux/arm64 explicitly, and `docker image inspect` is used to
# verify the architecture of what was actually saved rather than trusting the
# command to have worked.
#
# Requires: docker, ssh, kubectl, and passwordless (or agent) SSH to the nodes.

set -euo pipefail

REGISTRY="${SIDELOAD_REGISTRY:-192.168.1.50:30500}"
ARCH="${SIDELOAD_ARCH:-linux/arm64}"
NODES="${SIDELOAD_NODES:-pi2 pi3 pi4 pi7 raspberrypi}"
SSH_OPTS="-o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new"

usage() {
  sed -n '2,/^set -euo/p' "$0" | sed 's/^# \{0,1\}//; $d'
  exit "${1:-0}"
}

list_missing() {
  # Report the images that are actually blocking pods, with the nodes that need
  # them. This is the actionable set, and it is the same one the
  # `airgap-image-pull` preflight check reports.
  #
  # A fuller "what does each node hold" inventory is deliberately not attempted
  # here. Three ways of getting it were tried and none is usable:
  #
  #   * ssh needs key auth that is not configured;
  #   * `kubectl debug node/...` needs an image, and on the node you are trying
  #     to diagnose that image is the missing thing -- pi3 could not pull the
  #     k3s debugger image;
  #   * deriving the inventory from container status reports almost everything
  #     as missing, because a node legitimately lacks images it never runs. That
  #     report is technically true and useless.
  #
  # Once key auth exists, replace this with a per-node `k3s ctr images list`.
  local stuck
  stuck=$(kubectl get pods -A -o json | jq -r '
      .items[]
      | select(
          any(.status.containerStatuses[]?;
              ((.state.waiting.reason // "") | test("^(ImagePullBackOff|ErrImagePull)$")))
          or
          any(.status.initContainerStatuses[]?;
              ((.state.waiting.reason // "") | test("^(ImagePullBackOff|ErrImagePull)$")))
        )
      | "  \(.spec.nodeName // "?")\t" + ([.spec.containers[].image] | join(" "))
    ' | sort -u)
  if [ -z "$stuck" ]; then
    echo "  (none: no pod is currently blocked on an image pull)"
    return 0
  fi
  echo "$stuck" | while IFS=$'\t' read -r node images; do
    for img in $images; do
      printf '  %-64s %s\n' "$img" "$node"
    done
  done
}

main() {
  case "${1:-}" in
    --list|-l) list_missing; exit 0 ;;
    -h|--help|"") usage 0 ;;
  esac

  local only_node=""
  if [ "${1:-}" = "--node" ]; then
    only_node="$2"; shift 2
  fi

  [ $# -gt 0 ] || { usage 1; }

  # Pull everything first, verify architectures, and only then push and import.
  # Doing it per-image would leave the registry holding a half-finished set if
  # a later pull failed the architecture check.
  local tarballs=()
  local images=()
  for image in "$@"; do
    tarballs+=("$(pull_one "$image")") || exit 1
    images+=("$image")
  done

  local i
  for i in "${!images[@]}"; do
    push_one "${tarballs[$i]}" "${images[$i]}"
    for node in ${only_node:-$NODES}; do
      import_node "$node" "${tarballs[$i]}" || echo "WARNING: import into $node failed" >&2
    done
    rm -f "${tarballs[$i]}"
  done

  echo
  echo "done. verify with: $0 --list"
}

main "$@"
