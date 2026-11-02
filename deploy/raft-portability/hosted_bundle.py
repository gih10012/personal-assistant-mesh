"""Package a strict public artifact allowlist after the hosted build/test steps."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parent
TEMP = Path(os.environ["RUNNER_TEMP"])
TARGET = Path(os.environ["CARGO_TARGET_DIR"])
OUTPUT = Path(os.environ["AMITA_BUNDLE_DIR"])
TEST = "tests::durable_vote_survives_reconstruction"
if os.environ.get("GITHUB_ACTIONS") != "true":
    raise RuntimeError("this bundler records GitHub hosted evidence only")
if (TEMP / "amita-raft-hosted-test.passed").read_text().strip() != TEST:
    raise RuntimeError("native hosted test completion marker missing")
OUTPUT.mkdir(parents=True, exist_ok=False)


def capture(*args):
    return subprocess.check_output(args, text=True, cwd=ROOT).strip()


def copy_public(source, destination):
    source = source.resolve(strict=True)
    destination = OUTPUT / destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)


for name in ("libamita_raft_portability.rlib", "libamita_raft_portability.so"):
    copy_public(TARGET / "release" / name, Path("native") / name)
copy_public(TARGET / "wasm32-unknown-unknown/release/amita_raft_portability.wasm",
            "wasm/amita_raft_portability.raw.wasm")
for name in ("amita_raft_portability.js", "amita_raft_portability_bg.wasm",
             "amita_raft_portability.d.ts", "amita_raft_portability_bg.wasm.d.ts"):
    copy_public(TEMP / "amita-raft-generated-js" / name, Path("wasm") / name)
copy_public(ROOT / "Cargo.lock", "Cargo.lock")
copy_public(ROOT.parents[1] / "LICENSE", "licenses/amita-raft-portability/LICENSE")
for name in ("COPYRIGHT", "LICENSE-MIT", "LICENSE-APACHE"):
    copy_public(Path(os.environ["AMITA_TOOL_DIR"]) / f"rust-{name}",
                Path("licenses/rust-1.98.1") / name)

metadata = json.loads(capture("rustup", "run", "1.98.1", "cargo", "metadata",
                              "--locked", "--offline", "--format-version", "1"))
packages = []
for package in metadata["packages"]:
    directory = Path(package["manifest_path"]).parent.resolve()
    package_output = Path("licenses") / f"{package['name']}-{package['version']}"
    candidates = [p for p in directory.iterdir()
                  if p.is_file() and p.name.upper().startswith(("LICENSE", "COPYING", "NOTICE"))]
    if package.get("license_file"):
        candidates.append(directory / package["license_file"])
    copied = []
    for source in sorted(set(candidates)):
        # Do not let package metadata select a file outside its public crate.
        if not source.resolve().is_relative_to(directory):
            raise RuntimeError("license_file escaped crate directory")
        copy_public(source, package_output / source.name)
        copied.append(str(package_output / source.name))
    packages.append({"name": package["name"], "version": package["version"],
                     "license": package.get("license"), "license_files": copied})

files = [{"path": str(path.relative_to(OUTPUT)), "bytes": path.stat().st_size,
          "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
         for path in sorted(OUTPUT.rglob("*")) if path.is_file()]
if sum(item["bytes"] for item in files) > 20 * 1024 * 1024:
    raise RuntimeError("public delivery exceeded its bounded 20 MiB upload size")
repository, run_id = os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_RUN_ID"]
manifest = {
    "format": "amita-raft-hosted-build/1",
    "environment": "GitHub-hosted standard ubuntu-24.04; public repository job guard",
    "runner_image": {"os": os.environ.get("ImageOS"), "version": os.environ.get("ImageVersion")},
    "repository": repository, "source_commit": os.environ["GITHUB_SHA"],
    "source_ref": os.environ["GITHUB_REF"], "run_id": run_id,
    "run_attempt": os.environ["GITHUB_RUN_ATTEMPT"],
    "run_url": f"https://github.com/{repository}/actions/runs/{run_id}",
    "rustc": capture("rustup", "run", "1.98.1", "rustc", "--version", "--verbose"),
    "cargo": capture("rustup", "run", "1.98.1", "cargo", "--version"),
    "installed_targets": capture("rustup", "target", "list", "--installed", "--toolchain", "1.98.1"),
    "wasm_bindgen": "0.2.105; official archive SHA256 checked in workflow",
    "rust_license_source": "https://github.com/rust-lang/rust/tree/48a229ceaefd4985c50990b14116b6d856af0985",
    "hosted_native_case": {"name": TEST, "status": "passed before this packaging step"},
    "limits": "Not a local Click cargo test receipt, WASM runtime test, three-voter quorum test or DO deployment.",
    "packages": packages, "files": files,
}
(OUTPUT / "HOSTED-MANIFEST.json").write_text(json.dumps(manifest, indent=2) + "\n")
print(f"Prepared {len(files)} public artifact/license files from {len(packages)} packages.")
