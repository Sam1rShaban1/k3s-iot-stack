#!/usr/bin/env bash
# Import a verified image tarball into a node's containerd, using only
# Kubernetes access (no SSH).
#
# The nodes have no SSH key configured, and a repair tool that needs a
# credential the operator does not have cannot be the tool that fixes the
# problem. Kubernetes already authorises us as cluster-admin, so the import runs
# from a privileged pod on the target node instead.
#
# Three things had to be right, and each failed visibly first:
#
#   * the helper image must already exist on the node -- nothing can be pulled,
#     which is the problem being fixed. It is taken from an image the node is
#     already running, with its exact tag, and imagePullPolicy IfNotPresent. An
#     untagged reference silently resolves to :latest and the kubelet then
#     blocks on a pull that can never succeed.
#
#   * the helper container must run as root. The benthos image runs non-root,
#     and the kernel drops all capabilities on exec for a non-root user, so
#     `privileged: true` alone yields CapEff=0 and chroot/nsenter fail with
#     "Operation not permitted".
#
#   * chroot into the bind-mounted host root is refused, so the host's own
#     nsenter is invoked through the host's dynamic loader, with the host
#     library path, after entering PID 1's mount/user/net namespaces.
#
# Usage: import_image.sh <node> <tarball>

set -euo pipefail

NODE="$1"
TARBALL="$2"
POD="sideload-${NODE}"
NAMESPACE="${SIDELOAD_NAMESPACE:-kube-system}"

if [ ! -f "$TARBALL" ]; then
  echo "error: no such archive: $TARBALL" >&2
  exit 2
fi

# An image this node is already running, with its exact tag.
# Only Running pods qualify. Taking the image from a pod that is merely
# *scheduled* picks an image the node may not have -- which is how the first
# attempt chose the ArgoCD image on pi3, the very image pi3 is missing.
IMAGE=$(kubectl get pods -A -o json | jq -r --arg n "$NODE" '
    [ .items[]
      | select(.spec.nodeName == $n)
      | select(.status.phase == "Running")
      | .spec.containers[]
      | select(.image | test("busybox|pause") | not)
      | .image
    ] | .[0] // ""' 2>/dev/null)
if [ -z "$IMAGE" ]; then
  echo "error: could not find a helper image already present on $NODE" >&2
  echo "       (looked at the images of that node's running pods)" >&2
  exit 1
fi

echo "==> $NODE: helper image $IMAGE"

kubectl delete pod "$POD" -n "$NAMESPACE" --ignore-not-found >/dev/null 2>&1 || true
kubectl apply -f - >/dev/null <<EOF
apiVersion: v1
kind: Pod
metadata:
  name: $POD
  namespace: $NAMESPACE
spec:
  nodeName: $NODE
  hostPID: true
  restartPolicy: Never
  tolerations:
  - key: node-role.kubernetes.io/control-plane
    operator: Exists
    effect: NoSchedule
  containers:
  - name: c
    image: $IMAGE
    imagePullPolicy: IfNotPresent
    command: ["sleep", "1800"]
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

phase=""
for _ in $(seq 1 45); do
  phase=$(kubectl get pod "$POD" -n "$NAMESPACE" -o jsonpath='{.status.phase}' 2>/dev/null || true)
  case "$phase" in
    Running|Failed|Succeeded) break ;;
  esac
  sleep 2
done
if [ "$phase" != "Running" ]; then
  echo "error: helper pod on $NODE is '${phase:-unknown}', not Running" >&2
  kubectl describe pod "$POD" -n "$NAMESPACE" 2>/dev/null | sed -n '/Events:/,$p' | head -5 >&2
  kubectl delete pod "$POD" -n "$NAMESPACE" --ignore-not-found >/dev/null 2>&1 || true
  exit 1
fi

# The archive is streamed on stdin and never written to the node's disk, which
# matters when the card has tens of gigabytes free and the image is ~400 MB.
kubectl exec -i "$POD" -n "$NAMESPACE" -- \
  /host/lib/ld-linux-aarch64.so.1 \
  --library-path /host/lib/aarch64-linux-gnu:/host/usr/lib/aarch64-linux-gnu \
  /host/usr/bin/nsenter -t 1 -m -u -n -i -- \
  /usr/local/bin/k3s ctr images import - < "$TARBALL"

rc=$?
kubectl delete pod "$POD" -n "$NAMESPACE" --ignore-not-found >/dev/null 2>&1 || true
exit $rc
