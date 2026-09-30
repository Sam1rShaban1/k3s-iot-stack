#!/usr/bin/env python3
"""Report the architectures inside a `docker save` tarball.

`docker image inspect` is not trustworthy for this. On a host using the
containerd image store, `docker pull --platform linux/arm64 <image>` stores a
*manifest list*, and inspect then reports `Os=""` / `Architecture=""` for it. So
the natural verification -- "did I pull the right platform?" -- silently checks
nothing.

That matters here because the cluster is arm64 and this host is x86_64. Saving a
manifest list, or an amd64 image, produces an artifact that no Pi can run, and
the failure surfaces much later as `exec format error` or an endlessly
crash-looping pod rather than as an import error. The Longhorn
instance-manager image was shipped the wrong way for days on exactly this
mistake.

So this inspects the bytes that are about to be transferred: it reads
manifest.json and the config blob out of the tar and reports the platform of
every image in the archive. Exit status is non-zero unless exactly the
requested platform is present, so it can gate a transfer.
"""

from __future__ import annotations

import argparse
import json
import sys
import tarfile
from typing import Any


def _read_json(archive: tarfile.TarFile, name: str):
    try:
        handle = archive.extractfile(name)
    except KeyError:
        return None
    if handle is None:
        return None
    try:
        return json.loads(handle.read())
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


def _docker_format_platforms(archive: tarfile.TarFile) -> list[dict[str, Any]]:
    """Platforms from a classic `docker save` archive (manifest.json)."""
    manifest = _read_json(archive, "manifest.json")
    if not isinstance(manifest, list):
        return []
    found = []
    for entry in manifest:
        cfg_path = entry.get("Config")
        if not cfg_path:
            continue
        cfg = _read_json(archive, cfg_path)
        if isinstance(cfg, dict) and cfg.get("architecture"):
            found.append(
                {
                    "file": cfg_path,
                    "os": cfg.get("os", "?"),
                    "architecture": cfg["architecture"],
                }
            )
    return found


def _oci_platforms(archive: tarfile.TarFile) -> list[dict[str, Any]]:
    """Platforms from an OCI archive (index.json -> manifest -> config).

    A digest-pinned `docker pull` saved through Docker's containerd image store
    produces this shape: an OCI index whose single manifest is the requested
    platform. The index is a wrapper, not a multi-platform list, so the contents
    are correct even though the archive does not look single-platform at first
    glance.
    """
    index = _read_json(archive, "index.json")
    if not isinstance(index, dict):
        return []
    found = []
    for desc in index.get("manifests", []):
        digest = desc.get("digest", "")
        if not digest.startswith("sha256:"):
            continue
        manifest = _read_json(archive, digest)
        if not isinstance(manifest, dict):
            continue
        cfg_digest = (manifest.get("config") or {}).get("digest", "")
        if not cfg_digest.startswith("sha256:"):
            continue
        cfg = _read_json(archive, cfg_digest)
        if isinstance(cfg, dict) and cfg.get("architecture"):
            found.append(
                {
                    "file": cfg_digest,
                    "os": cfg.get("os", "?"),
                    "architecture": cfg["architecture"],
                }
            )
    return found


def _platforms(archive: tarfile.TarFile) -> list[dict[str, Any]]:
    """Every (os, architecture) in the archive, whichever format it is."""
    return _docker_format_platforms(archive) or _oci_platforms(archive)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("archive", help="path to a docker save tarball")
    ap.add_argument(
        "--require",
        default="linux/arm64",
        help="platform that must be present, as os/arch (default: linux/arm64)",
    )
    args = ap.parse_args()

    want_os, _, want_arch = args.require.partition("/")

    try:
        with tarfile.open(args.archive, "r:*") as archive:
            found = _platforms(archive)
            manifest_raw = None
            for member in archive.getmembers():
                if member.isfile() and member.name.endswith(
                    ("manifest.json", "index.json", "oci-layout")
                ):
                    handle = archive.extractfile(member)
                    if handle is not None:
                        manifest_raw = handle.read()[:200].decode(errors="replace")
                    break
    except FileNotFoundError:
        print(f"error: no such archive: {args.archive}", file=sys.stderr)
        return 2
    except tarfile.TarError as e:
        print(f"error: not a readable tar archive: {e}", file=sys.stderr)
        return 2

    if not found:
        print(
            f"error: no image config found in {args.archive}. "
            "If this is a manifest list, docker saved the index rather than a "
            "single platform; pull with --platform and re-save.",
            file=sys.stderr,
        )
        if manifest_raw:
            print(f"       archive starts with: {manifest_raw!r}", file=sys.stderr)
        return 3

    print(f"archive: {args.archive}")
    for item in sorted(found, key=lambda d: (d["os"], d["architecture"])):
        print(f"  {item['os']}/{item['architecture']}  ({item['file']})")

    have = {f"{i['os']}/{i['architecture']}" for i in found}
    if args.require in have:
        print(f"OK: contains {args.require}")
        return 0

    print(
        f"FAIL: does not contain {args.require}; it contains {sorted(have)}",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
