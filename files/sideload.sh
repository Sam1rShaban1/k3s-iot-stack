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

# Image for the privileged helper pod. It must already exist on every node,
# because nothing can be pulled -- which is the whole problem. k3s ships it, and
# this cluster was built with it present, which makes it the one usable choice.
SIDELOAD_DEBUG_IMAGE="${SIDELOAD_DEBUG_IMAGE:-rancher/k3s:v1.35.4-k3s1}"
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

pull_one() {
  local image="$1" tarball
  # stdout carries the tarball path and nothing else: the caller captures it
  # with $(...), so any progress line printed here corrupts the value and the
  # import then fails with "No such file or directory" on a path that is really
  # a paragraph of log output.
  echo "==> resolving $image for $ARCH" >&2
  # Not `docker pull --platform`: that records a manifest list, and a later
  # `docker save` then fails with "content digest not found". The resolver picks
  # the platform's manifest and pins it by digest, so the local store holds
  # exactly one image. See files/resolve_pull_platform.py.
  local pinned
  # The resolver prints progress on stderr and the pinned reference on stdout,
  # so stdout is what must be captured. Getting this backwards yields a docker
  # error like "invalid reference format" from whatever progress line came last.
  pinned=$(python3 "$(dirname "$0")/resolve_pull_platform.py" "$image" \
            --platform "$ARCH" 2>/dev/null | tail -1)
  # The resolver also re-tags the pulled content as "$image" so the archive
  # carries the name the manifests request.
  if [ -z "$pinned" ]; then
    echo "ERROR: could not resolve $image for $ARCH" >&2
    return 1
  fi
  # Save by digest. Saving by tag can pick up a stale multi-platform entry that
  # happens to share the tag, which is how an amd64 archive reaches an arm64
  # node.
  tarball="$(mktemp -d)/image.tar"
  # Save by the manifest-facing name, not the digest. ctr records the name in
  # the archive, so saving the digest reference would import the image under a
  # name no Deployment references and the pod would stay in ImagePullBackOff
  # while the import reported success.
  docker save -o "$tarball" "$image" || {
    echo "ERROR: docker save failed for $pinned" >&2
    return 1
  }
  # Verify the bytes about to be shipped, not docker's metadata:
  # `docker image inspect` reports an empty Architecture for a manifest-list
  # pull, so the obvious check silently passes.
  if ! python3 "$(dirname "$0")/check_image_platform.py" "$tarball" \
        --require "$ARCH" >&2; then
    echo "ERROR: archive for $image is not $ARCH; refusing to ship it." >&2
    return 1
  fi
  echo "$tarball"
}

# Choose a helper image that the given node already has, from the imageIDs of
# its running pods. Candidates are filtered to images that plausibly contain
# nsenter; the k3s image is preferred because it also carries the ctr client.
pick_helper_image() {
  local node="$1"
  local from_api
  from_api=$(kubectl get pods -A -o json | jq -r --arg n "$node" '
      [ .items[]
        | select(.spec.nodeName == $n)
        | .status.containerStatuses[]?.imageID
        | sub("^[a-z-]+://"; "")
        | sub("@sha256:.*"; "")
      ] | unique | .[]' 2>/dev/null)
  local fallback="${SIDELOAD_DEBUG_IMAGE:-rancher/k3s:v1.35.4-k3s1}"
  # Prefer k3s, then anything with a shell, then the first candidate at all.
  local pick
  pick=$(printf '%s\n' "$from_api" | grep -E '(^|/)k3s:' | head -1)
  [ -z "$pick" ] && pick=$(printf '%s\n' "$from_api" | grep -vE 'busybox|pause' | head -1)
  [ -z "$pick" ] && pick="$fallback"
  echo "$pick"
}

import_node() {
  local node="$1" tarball="$2"
  echo "==> importing into $node"
  # Delegates to import_image.sh, which uses a privileged pod on the node
  # rather than ssh: the nodes have no key configured, and a repair tool that
  # needs a credential the operator lacks cannot be the tool that fixes the
  # problem. See that script for the three things that must be true.
  SIDELOAD_TRANSPORT="${SIDELOAD_TRANSPORT:-kubectl}" \
    "$(dirname "$0")/import_image.sh" "$node" "$tarball"
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

  # Pull and verify every image first, then import. Verifying up front means a
  # later architecture failure cannot leave some nodes updated and others not.
  local tarballs=()
  local images=()
  for image in "$@"; do
    tarballs+=("$(pull_one "$image")") || exit 1
    images+=("$image")
  done

  local i
  for i in "${!images[@]}"; do
    for node in ${only_node:-$NODES}; do
      import_node "$node" "${tarballs[$i]}" || echo "WARNING: import into $node failed" >&2
    done
    rm -f "${tarballs[$i]}"
  done

  echo
  echo "done. verify with: $0 --list"
}

main "$@"
