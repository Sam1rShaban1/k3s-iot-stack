#!/usr/bin/env bash
# Configure the in-cluster registry as an image mirror on a node.
#
# Why this exists
# ---------------
# The cluster is air-gapped, so images must be moved in by hand. The obvious
# method -- `docker save` on the workstation, then stream the archive into the
# node -- does not work at this size: the Longhorn image is 378 MB, and pushing
# that through `kubectl exec -i` hangs, because the API server proxies the exec
# stream and buffers it badly. The import also has to be repeated per node.
#
# The registry is already reachable from pods on every node (an earlier check
# wrongly concluded otherwise: that probe ran from a pod in the `nats`
# namespace, whose NetworkPolicy blocks the registry port -- so it measured the
# policy, not the network). So the registry can serve the images, and containerd
# only needs to be told it may.
#
# A mirror is the right shape for this: with `mirrors` configured, containerd
# asks the mirror first for ANY image, and the mirror is populated by pushing the
# same repository path into it. Manifests keep referencing upstream names such as
# quay.io/argoproj/argocd, and no manifest has to be rewritten per environment.
#
# The mirror is HTTP, so it is declared explicitly as such; without that,
# containerd refuses a non-TLS endpoint.
#
# Usage:
#   configure_mirror.sh <node> [restart]
#     Write registries.yaml. Pass "restart" to restart k3s on that node so the
#     change takes effect -- needed once, and the pods on that node are
#     rescheduled while it happens.
#
#   configure_mirror.sh --all
#     Write the config on every node without restarting anything. Roll the
#     restarts separately, one node at a time, so a problem is visible on one
#     node rather than cluster-wide.

set -euo pipefail

NAMESPACE="${SIDELOAD_NAMESPACE:-kube-system}"
# The registry lives on the control-plane node. Address it by its STATIC fabric
# address, not its LAN address.
#
# 192.168.1.50 was a DHCP lease on the home WLAN and it moved. When it did, every
# node in the cluster silently lost its mirror -- which showed up as
# ImagePullBackOff on any pod that needed an image not already cached, on
# whichever node it happened to land. 10.0.0.1 is the address configured in
# ansible/inventory.ini, does not move, and is reachable from every node.
#
# Overridable for a genuinely air-gapped site, where the registry will be on a
# fixed address of its own.
MIRROR="${SIDELOAD_MIRROR:-10.0.0.1:30500}"
HELPER_IMAGE="${SIDELOAD_DEBUG_IMAGE:-jeffail/benthos:4.1.0}"
REGISTRIES_PATH=/etc/rancher/k3s/registries.yaml

# Upstream registries whose images this cluster runs. Listing them explicitly
# keeps the mirror from being consulted for registries the cluster never pulls
# from, which would add a pointless lookup to every pod start.
MIRRORS=(docker.io quay.io registry.k8s.io ghcr.io public.ecr.aws gcr.io)

render_registries_yaml() {
  echo "# Managed by files/configure_mirror.sh -- do not edit by hand."
  echo "#"
  echo "# The cluster is air-gapped. These mirrors let images be delivered by"
  echo "# pushing into the in-cluster registry instead of streaming a tarball"
  echo "# into every node's containerd."
  echo "mirrors:"
  for m in "${MIRRORS[@]}"; do
    cat <<EOF
  "$m":
    endpoint:
      - "http://${MIRROR}"
EOF
  done
  echo "configs:"
  echo "  \"${MIRROR}\":"
  echo "    tls:"
  echo "      insecure_skip_verify: true"
}

# Run a shell command in the host's namespaces on the given node.
on_node() {
  local node="$1"; shift
  local pod="nodeconf-${node}"
  kubectl delete pod "$pod" -n "$NAMESPACE" --ignore-not-found >/dev/null 2>&1 || true
  kubectl apply -f - >/dev/null <<EOF
apiVersion: v1
kind: Pod
metadata:
  name: $pod
  namespace: $NAMESPACE
spec:
  nodeName: $node
  hostPID: true
  restartPolicy: Never
  tolerations:
  - key: node-role.kubernetes.io/control-plane
    operator: Exists
    effect: NoSchedule
  containers:
  - name: c
    image: $HELPER_IMAGE
    imagePullPolicy: IfNotPresent
    command: ["sleep", "1200"]
    securityContext:
      privileged: true
      runAsUser: 0
      runAsGroup: 0
    volumeMounts:
    - name: host
      mountPath: /host
  volumes:
  - name: host
    hostPath:
        path: /
        type: Directory
EOF
  local phase=""
  for _ in $(seq 1 45); do
    phase=$(kubectl get pod "$pod" -n "$NAMESPACE" -o jsonpath='{.status.phase}' 2>/dev/null || true)
    case "$phase" in Running|Failed|Succeeded) break ;; esac
    sleep 2
  done
  if [ "$phase" != "Running" ]; then
    echo "  $node: helper pod did not start (${phase:-unknown})" >&2
    kubectl delete pod "$pod" -n "$NAMESPACE" --ignore-not-found >/dev/null 2>&1 || true
    return 1
  fi
  kubectl exec -i "$pod" -n "$NAMESPACE" -- \
    /host/lib/ld-linux-aarch64.so.1 \
    --library-path /host/lib/aarch64-linux-gnu:/host/usr/lib/aarch64-linux-gnu \
    /host/usr/bin/nsenter -t 1 -m -u -n -i -- /bin/sh -c "$1"
  local rc=$?
  kubectl delete pod "$pod" -n "$NAMESPACE" --ignore-not-found >/dev/null 2>&1 || true
  return $rc
}

configure() {
  local node="$1" restart="${2:-}"
  local yaml; yaml=$(render_registries_yaml)
  echo "==> $node: writing $REGISTRIES_PATH"
  # Written through a heredoc into the host filesystem. The content is
  # generated above, never interpolated from a variable, so nothing in it can
  # be re-interpreted by the remote shell.
  if ! printf '%s\n' "$yaml" | on_node "$node" \
        "mkdir -p /etc/rancher/k3s && cat > $REGISTRIES_PATH"; then
    echo "  $node: FAILED to write config" >&2
    return 1
  fi
  if [ "$restart" = "restart" ]; then
    echo "==> $node: restarting k3s (pods here will be rescheduled)"
    on_node "$node" \
      "systemctl restart k3s 2>/dev/null || systemctl restart k3s-agent" || {
      echo "  $node: restart FAILED -- check the node" >&2
      return 1
    }
  fi
  echo "  $node: ok"
}

main() {
  if [ "${1:-}" = "--all" ]; then
    shift
    local rc=0
    for node in "$@"; do
      configure "$node" || rc=1
    done
    return $rc
  fi
  [ $# -ge 1 ] || { echo "usage: $0 <node> [restart] | $0 --all <node>..." >&2; exit 2; }
  configure "$@"
}

main "$@"
