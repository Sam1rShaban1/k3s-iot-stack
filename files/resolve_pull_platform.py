#!/usr/bin/env python3
"""Resolve an image reference to a single-platform digest, and pull it.

The air-gapped cluster is arm64; this host is x86_64. The naive approach --
`docker pull --platform linux/arm64 img` then `docker save` -- does not work
with Docker's containerd image store: the pull records a *manifest list*, and
`docker save` then fails with

    unable to create manifests file: NotFound: content digest sha256:... not found

because the platform-specific content was never materialised locally. Worse,
`docker image inspect` reports `Os=""` and `Architecture=""` for such a pull, so
the obvious verification silently checks nothing. An amd64 or multi-platform
archive that reaches an arm64 node fails much later, as an exec format error or
an endlessly crash-looping pod. The Longhorn instance-manager image was shipped
this way and lost for days.

The fix is to resolve the manifest list, find the entry for the target platform,
and pull *that digest*. A digest names exactly one manifest, so the local store
holds one image and `docker save` produces a single-platform archive that
`files/check_image_platform.py` can verify.

Usage:
    resolve_pull_platform.py <image[:tag]> [--platform linux/arm64]
    resolve_pull_platform.py --digest-only <image[:tag]> [--platform ...]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.error
import urllib.request

ACCEPT = ", ".join(
    [
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
    ]
)

# Anonymous pull tokens, keyed by registry host. Only registries that actually
# require one are listed; everything else is fetched unauthenticated.
TOKEN_URLS = {
    "registry-1.docker.io": (
        "https://auth.docker.io/token?service=registry.docker.io&scope="
        "repository:{repository}:pull"
    ),
    "quay.io": "https://quay.io/v2/auth?service=quay.io&scope=repository:{repository}:pull",
}


def split_reference(image: str) -> tuple[str, str, str]:
    """Return (registry, repository, tag_or_digest)."""
    ref = image
    digest = ""
    if "@" in ref:
        ref, digest = ref.split("@", 1)

    registry = "registry-1.docker.io"
    first, _, rest = ref.partition("/")
    if "/" in rest or ("." in first or ":" in first or first == "localhost"):
        registry, ref = first, rest or first
    elif first == "library" or True:
        # docker.io shorthand: official images live under library/
        if "/" not in ref:
            ref = f"library/{ref}"
        registry = "registry-1.docker.io"

    tag = digest or "latest"
    if not digest and ":" in ref.rsplit("/", 1)[-1]:
        ref, tag = ref.rsplit(":", 1)
    return registry, ref, tag


def _token(registry: str, repository: str) -> str:
    template = TOKEN_URLS.get(registry)
    if not template:
        return ""
    url = template.format(repository=repository)
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            return json.load(resp).get("token", "")
    except Exception:  # noqa: BLE001
        return ""


def fetch_manifest(registry: str, repository: str, reference: str) -> tuple[dict, str]:
    url = f"https://{registry}/v2/{repository}/manifests/{reference}"
    req = urllib.request.Request(url, headers={"Accept": ACCEPT})
    token = _token(registry, repository)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.load(resp), resp.headers.get("Docker-Content-Digest", "")
    except urllib.error.HTTPError as e:
        raise SystemExit(f"error: registry {registry} returned {e.code} for {url}") from e
    except urllib.error.URLError as e:
        raise SystemExit(f"error: cannot reach {registry}: {e.reason}") from e


def resolve(image: str, want_os: str, want_arch: str) -> tuple[str, str, str]:
    """Return (registry, repository, digest-for-platform)."""
    registry, repository, tag = split_reference(image)
    doc, header_digest = fetch_manifest(registry, repository, tag)

    if "manifests" not in doc:
        # Already a single manifest. Its own digest is the answer, but the
        # config has to be read to know whether it is the right platform.
        cfg_digest = (doc.get("config") or {}).get("digest")
        platform = _platform_of(registry, repository, cfg_digest) if cfg_digest else None
        digest = header_digest or doc.get("digest", "")
        if platform and platform != f"{want_os}/{want_arch}":
            raise SystemExit(
                f"error: {image} is a single {platform} manifest, but "
                f"{want_os}/{want_arch} was requested. There is no "
                f"{want_os}/{want_arch} variant of this tag."
            )
        return registry, repository, digest

    available = []
    for entry in doc["manifests"]:
        plat = entry.get("platform") or {}
        # Skip attestation manifests, which carry no runnable image.
        if plat.get("os") == "unknown" or plat.get("architecture") == "unknown":
            continue
        os_name = plat.get("os")
        arch = plat.get("architecture")
        # Match on os/arch only. Registry manifest lists frequently spell arm64
        # as "linux/arm64/v8"; treating the variant as part of the platform made
        # a perfectly good arm64 image look unavailable.
        available.append((f"{os_name}/{arch}", entry.get("digest")))

    for label, digest in available:
        if label == f"{want_os}/{want_arch}":
            return registry, repository, digest

    listing = ", ".join(sorted({lbl for lbl, _ in available})) or "none"
    raise SystemExit(
        f"error: {image} has no {want_os}/{want_arch} variant. "
        f"Available: {listing}"
    )


def _platform_of(registry: str, repository: str, digest: str) -> str:
    url = f"https://{registry}/v2/{repository}/blobs/{digest}"
    req = urllib.request.Request(url)
    token = _token(registry, repository)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            cfg = json.load(resp)
    except Exception:  # noqa: BLE001
        return ""
    return f"{cfg.get('os', '?')}/{cfg.get('architecture', '?')}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("image", help="image reference, e.g. quay.io/argoproj/argocd:v3.3.6")
    ap.add_argument(
        "--platform",
        default="linux/arm64",
        help="target platform as os/arch (default: linux/arm64)",
    )
    ap.add_argument(
        "--digest-only",
        action="store_true",
        help="print repo@digest instead of pulling",
    )
    args = ap.parse_args()

    want_os, _, want_arch = args.platform.partition("/")
    registry, repository, digest = resolve(args.image, want_os, want_arch)
    pinned = f"{registry}/{repository}@{digest}"

    if args.digest_only:
        print(pinned)
        return 0

    print(f"resolved {args.image} -> {args.platform} -> {digest}", file=sys.stderr)
    print(f"pulling {pinned}", file=sys.stderr)
    # Pulling by digest names one manifest, so the local store holds one image
    # and `docker save` yields a single-platform archive.
    res = subprocess.run(
        ["docker", "pull", "--platform", args.platform, pinned],
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        sys.stderr.write(res.stderr)
        return res.returncode
    # Tag with the reference the manifests actually ask for.
    #
    # This matters more than it looks. `k3s ctr images import` records whatever
    # name the archive carries, so tagging a placeholder like ":sideloaded"
    # imports the image under a name no manifest references, and the pod stays
    # in ImagePullBackOff while the import reports success. That is exactly what
    # happened: the first Longhorn import "saved" cleanly and the detector still
    # reported the image missing on that node.
    subprocess.run(["docker", "tag", pinned, args.image], check=True)
    print(args.image)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
