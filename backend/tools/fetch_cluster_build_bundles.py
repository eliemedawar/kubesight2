#!/usr/bin/env python3
"""Populate ``backend/api/data/{cni,addons}`` from the Cluster Builder catalog.

Every CNI and add-on manifest the builder can install is pinned to a URL and a
SHA-256 digest in its descriptor. At build time the executor prefers a bundled
copy and only falls back to the pinned URL when the repo mode allows it, so a
populated data directory is what makes ``offline`` mode — and any network that
cannot reach GitHub — work.

Usage (from ``backend/``)::

    python tools/fetch_cluster_build_bundles.py              # download all
    python tools/fetch_cluster_build_bundles.py --verify     # check, no writes
    python tools/fetch_cluster_build_bundles.py --only calico,metallb
    python tools/fetch_cluster_build_bundles.py --from-dir ~/downloads
    python tools/fetch_cluster_build_bundles.py --cilium     # needs helm

``--from-dir`` imports manifests fetched by some other tool (a browser, curl,
an air-gapped transfer) instead of downloading them; the digests are still
enforced, so an imported file is exactly as trustworthy as a downloaded one.

Cilium publishes no single-file manifest, so ``--cilium`` renders one with
``helm template``. That is the only entry that needs a tool beyond Python.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import ssl
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Sequence

_BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from api.services.cluster_build import addons as addon_registry  # noqa: E402
from api.services.cluster_build.cni import _ALL as _ALL_CNI  # noqa: E402
from api.services.cluster_build.cni.cilium import (  # noqa: E402
    CILIUM,
    CILIUM_CHART_REPO,
    CILIUM_HELM_VALUES,
)

_DATA_DIR = _BACKEND_DIR / "api" / "data"
_TIMEOUT_SECONDS = 120


@dataclass(frozen=True)
class Pin:
    kind: str          # cni | addons
    component: str
    version: str
    filename: str
    url: str
    sha256: str

    @property
    def destination(self) -> Path:
        return _DATA_DIR / self.kind / self.component / self.version / self.filename

    @property
    def label(self) -> str:
        return f"{self.component} {self.version} {self.filename}"


def _pins() -> List[Pin]:
    pins: List[Pin] = []
    for descriptor in addon_registry.available():
        for version in descriptor.versions:
            for filename, url, digest in zip(
                descriptor.manifest_files,
                descriptor.manifest_urls,
                descriptor.manifest_sha256,
            ):
                pins.append(Pin(
                    kind="addons",
                    component=descriptor.id,
                    version=version,
                    filename=filename,
                    url=url.format(version=version),
                    sha256=digest,
                ))
    for descriptor in _ALL_CNI.values():
        if not descriptor.manifest_urls:
            continue  # bundled-only (Cilium) — see --cilium.
        for version in descriptor.versions:
            digests = descriptor.manifest_sha256.get(version, ())
            for index, filename in enumerate(descriptor.manifest_files):
                pins.append(Pin(
                    kind="cni",
                    component=descriptor.id,
                    version=version,
                    filename=filename,
                    url=descriptor.manifest_urls[index].format(version=version),
                    sha256=digests[index] if index < len(digests) else "",
                ))
    return pins


def _opener(ca_bundle: Optional[str]) -> urllib.request.OpenerDirector:
    context = ssl.create_default_context(cafile=ca_bundle) if ca_bundle \
        else ssl.create_default_context()
    proxies = {}
    for scheme in ("http", "https"):
        value = os.environ.get(f"{scheme}_proxy") or os.environ.get(f"{scheme.upper()}_PROXY")
        if value:
            proxies[scheme] = value
    return urllib.request.build_opener(
        urllib.request.ProxyHandler(proxies or None),
        urllib.request.HTTPSHandler(context=context),
    )


def _source_in(directory: Path, pin: Pin) -> Optional[Path]:
    """Accept either the on-disk layout or a flat ``<id>-<version>-<file>`` dump."""
    candidates = (
        directory / pin.kind / pin.component / pin.version / pin.filename,
        directory / pin.component / pin.version / pin.filename,
        directory / f"{pin.component}-{pin.version}-{pin.filename}",
        directory / pin.filename,
    )
    return next((path for path in candidates if path.is_file()), None)


def _write(pin: Pin, content: bytes) -> None:
    pin.destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = pin.destination.with_suffix(pin.destination.suffix + ".part")
    temporary.write_bytes(content)
    temporary.replace(pin.destination)


def _process(
    pin: Pin,
    *,
    verify_only: bool,
    force: bool,
    from_dir: Optional[Path],
    opener: urllib.request.OpenerDirector,
) -> str:
    """Return one of ok | written | mismatch | missing | error, and report it."""
    if pin.destination.is_file() and not force:
        digest = hashlib.sha256(pin.destination.read_bytes()).hexdigest()
        if digest == pin.sha256:
            print(f"  ok       {pin.label}")
            return "ok"
        print(f"  MISMATCH {pin.label}")
        print(f"           bundled {digest}")
        print(f"           pinned  {pin.sha256}")
        print("           re-run with --force to replace it")
        return "mismatch"

    if verify_only:
        print(f"  MISSING  {pin.label}")
        return "missing"

    if from_dir is not None:
        source = _source_in(from_dir, pin)
        if source is None:
            print(f"  MISSING  {pin.label} (not found under {from_dir})")
            return "missing"
        content = source.read_bytes()
        origin = str(source)
    else:
        try:
            with opener.open(pin.url, timeout=_TIMEOUT_SECONDS) as response:
                content = response.read()
        except (urllib.error.URLError, OSError) as exc:
            print(f"  ERROR    {pin.label}: {exc}")
            print(f"           {pin.url}")
            return "error"
        origin = pin.url

    digest = hashlib.sha256(content).hexdigest()
    if digest != pin.sha256:
        print(f"  MISMATCH {pin.label}")
        print(f"           fetched {digest} from {origin}")
        print(f"           pinned  {pin.sha256}")
        return "mismatch"

    _write(pin, content)
    print(f"  written  {pin.label} ({len(content)} bytes)")
    return "written"


def _render_cilium(helm: str, chart_version: str, verify_only: bool) -> str:
    destination = _DATA_DIR / "cni" / CILIUM.id / chart_version / CILIUM.manifest_files[0]
    if verify_only:
        state = "ok" if destination.is_file() else "MISSING"
        print(f"  {state:<8} cilium {chart_version} {CILIUM.manifest_files[0]}")
        return "ok" if destination.is_file() else "missing"

    command: List[str] = [
        helm, "template", "cilium", "cilium/cilium",
        "--version", chart_version,
        "--namespace", "kube-system",
    ]
    for key, value in CILIUM_HELM_VALUES.items():
        command += ["--set", f"{key}={value}"]
    print(f"  running  {' '.join(command)}")
    try:
        subprocess.run(
            [helm, "repo", "add", "cilium", CILIUM_CHART_REPO],
            check=True, capture_output=True, text=True,
        )
        subprocess.run(
            [helm, "repo", "update", "cilium"],
            check=True, capture_output=True, text=True,
        )
        result = subprocess.run(command, check=True, capture_output=True, text=True)
    except FileNotFoundError:
        print(f"  ERROR    cilium: '{helm}' not found — install helm or pass --helm")
        return "error"
    except subprocess.CalledProcessError as exc:
        print(f"  ERROR    cilium: helm exited {exc.returncode}")
        print((exc.stderr or "").strip()[:2000])
        return "error"

    content = result.stdout.encode("utf-8")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(content)
    print(f"  written  cilium {chart_version} {CILIUM.manifest_files[0]} "
          f"({len(content)} bytes, sha256 {hashlib.sha256(content).hexdigest()})")
    return "written"


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Download and verify Cluster Builder CNI/add-on manifests.",
    )
    parser.add_argument(
        "--only",
        help="Comma-separated component IDs to process (default: all).",
    )
    parser.add_argument(
        "--verify", action="store_true", dest="verify_only",
        help="Report what is bundled and whether it matches the pins; write nothing.",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Re-fetch and overwrite manifests that are already bundled.",
    )
    parser.add_argument(
        "--from-dir", type=Path,
        help="Import manifests from a local directory instead of downloading. "
             "Digests are still enforced.",
    )
    parser.add_argument(
        "--ca-bundle",
        help="PEM file of extra trust anchors, for TLS-inspecting proxies. "
             "http_proxy/https_proxy are honoured from the environment.",
    )
    parser.add_argument(
        "--cilium", action="store_true",
        help=f"Also render Cilium {CILIUM.versions[0]} with `helm template`.",
    )
    parser.add_argument(
        "--helm", default="helm", help="helm executable to use with --cilium.",
    )
    args = parser.parse_args(argv)

    selected = {item.strip() for item in (args.only or "").split(",") if item.strip()}
    pins: Iterable[Pin] = _pins()
    if selected:
        known = {pin.component for pin in _pins()} | {CILIUM.id}
        unknown = selected - known
        if unknown:
            parser.error(
                f"Unknown component(s): {', '.join(sorted(unknown))}. "
                f"Known: {', '.join(sorted(known))}."
            )
        pins = [pin for pin in pins if pin.component in selected]

    if args.from_dir is not None and not args.from_dir.is_dir():
        parser.error(f"--from-dir {args.from_dir} is not a directory.")

    print(f"Bundle directory: {_DATA_DIR}")
    opener = _opener(args.ca_bundle)
    outcomes = [
        _process(
            pin,
            verify_only=args.verify_only,
            force=args.force,
            from_dir=args.from_dir,
            opener=opener,
        )
        for pin in pins
    ]

    if args.cilium or (selected and CILIUM.id in selected):
        outcomes.append(
            _render_cilium(args.helm, CILIUM.versions[0], args.verify_only)
        )

    counts = {outcome: outcomes.count(outcome) for outcome in set(outcomes)}
    summary = ", ".join(f"{count} {name}" for name, count in sorted(counts.items()))
    print(f"\n{len(outcomes)} manifest(s): {summary or 'nothing to do'}")
    failed = sum(
        count for name, count in counts.items()
        if name in ("mismatch", "missing", "error")
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
