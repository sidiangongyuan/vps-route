"""Build a pinned gh for macOS; do not install it or access credentials."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request


GH_VERSION = "2.93.0"
GO_VERSION = "go1.27.3"
SOURCE_URL = f"https://codeload.github.com/cli/cli/tar.gz/refs/tags/v{GH_VERSION}"
SOURCE_SHA256 = "8f3369ade41fe7a04ff93ec6029d5b0a8a94ccb4bb59f338d5c445aa695f0753"


def build_environment():
    architecture = {"arm64": "arm64", "x86_64": "amd64"}.get(platform.machine())
    if architecture is None:
        raise RuntimeError("unsupported macOS architecture")
    # Keep transport and process settings, but do not inherit Go build settings.
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("GO", "CGO_"))}
    env.update(GOOS="darwin", GOARCH=architecture, GOWORK="off",
               GOTOOLCHAIN="local", GOENV="off", GOFLAGS="", CGO_ENABLED="0",
               GOPROXY="https://proxy.golang.org", GOSUMDB="sum.golang.org",
               GOPRIVATE="", GONOPROXY="", GONOSUMDB="",
               SSL_CERT_FILE="/etc/ssl/cert.pem", GODEBUG="x509sslcertoverrideplatform=1")
    env.pop("SSL_CERT_DIR", None)
    return env


def verify_target(compiler, binary, env):
    metadata = subprocess.check_output([str(compiler), "version", "-m", str(binary)],
                                       env=env, text=True)
    settings = dict(line.strip().removeprefix("build\t").split("=", 1)
                    for line in metadata.splitlines() if line.strip().startswith("build\t")
                    and "=" in line)
    for key in ("GOOS", "GOARCH", "CGO_ENABLED"):
        if settings.get(key) != env[key]:
            raise RuntimeError(f"built gh has an unexpected {key}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--go", required=True, type=Path, help="Go 1.27.3 executable")
    parser.add_argument("--output", required=True, type=Path, help="new output directory")
    args = parser.parse_args()
    if sys.platform != "darwin":
        parser.error("build this artifact on macOS")
    compiler = args.go.resolve(strict=True)
    output = args.output.absolute()
    if output.exists() or output.is_symlink():
        parser.error("output must not exist")
    env = build_environment()
    version = subprocess.check_output([str(compiler), "version"], env=env, text=True).strip()
    if version.split()[2] != GO_VERSION:
        parser.error(f"requires {GO_VERSION}")
    with tempfile.TemporaryDirectory(prefix="vps-route-gh-build-") as directory:
        root = Path(directory)
        request = urllib.request.Request(SOURCE_URL, headers={"User-Agent": "vps-route-gh-build"})
        with urllib.request.urlopen(request, timeout=60) as response:
            archive = response.read(40_000_001)
        if len(archive) > 40_000_000 or hashlib.sha256(archive).hexdigest() != SOURCE_SHA256:
            raise RuntimeError("gh source archive checksum mismatch")
        tar_path = root / "source.tar.gz"
        tar_path.write_bytes(archive)
        with tarfile.open(tar_path) as source:
            source.extractall(root / "source", filter="data")
        env.update(GOCACHE=str(root / "cache"), GOMODCACHE=str(root / "modules"),
                   GOPATH=str(root / "gopath"))
        binary = root / "gh"
        command = [str(compiler), "build", "-mod=readonly", "-buildvcs=false", "-trimpath",
                   "-ldflags", f"-X github.com/cli/cli/v2/internal/build.Version={GH_VERSION}-vps-route",
                   "-o", str(binary), "./cmd/gh"]
        subprocess.run(command, cwd=root / "source" / f"cli-{GH_VERSION}", env=env, check=True)
        verify_target(compiler, binary, env)
        output.mkdir(mode=0o700)
        shutil.copyfile(binary, output / "gh")
        (output / "gh").chmod(0o700)
        manifest = {"source": SOURCE_URL, "source_sha256": SOURCE_SHA256,
                    "go": version, "gh": GH_VERSION, "cgo_enabled": False,
                    "target": f"{env['GOOS']}/{env['GOARCH']}",
                    "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest()}
        (output / "build.json").write_text(json.dumps(manifest, indent=2) + "\n")
        print(f"Built {output / 'gh'}; test before installing.")


if __name__ == "__main__":
    main()
