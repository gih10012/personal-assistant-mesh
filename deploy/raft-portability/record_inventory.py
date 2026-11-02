"""Generate build-delivery metadata; this does not execute or certify any test."""
import hashlib
import json
from pathlib import Path
import tomllib

ROOT = Path(__file__).resolve().parent
paths = [
    "Cargo.lock",
    "target/release/libamita_raft_portability.rlib",
    "target/release/libamita_raft_portability.so",
    "target/wasm32-unknown-unknown/release/amita_raft_portability.wasm",
    "pkg/amita_raft_portability.js",
    "pkg/amita_raft_portability_bg.wasm",
    ".tools/downloads/rust-std-1.98.1-wasm32-unknown-unknown.tar.xz",
    ".tools/downloads/wasm-bindgen-0.2.105-x86_64-unknown-linux-musl.tar.gz",
    ".tools/wasm-bindgen-0.2.105-x86_64-unknown-linux-musl/wasm-bindgen",
]
artifacts = []
for name in paths:
    path = ROOT / name
    if path.is_file():
        artifacts.append({"path": name, "bytes": path.stat().st_size,
                          "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
lock = tomllib.loads((ROOT / "Cargo.lock").read_text())
registry = Path.home() / ".cargo" / "registry" / "src"
packages = []
for package in lock["package"]:
    item = {key: package[key] for key in ("name", "version", "source", "checksum") if key in package}
    manifests = list(registry.glob(f"*/{package['name']}-{package['version']}/Cargo.toml"))
    if package["name"] == "amita-raft-portability":
        manifests = [ROOT / "Cargo.toml"]
    if manifests:
        meta = tomllib.loads(manifests[0].read_text())["package"]
        item["license"] = meta.get("license")
        item["license_file"] = meta.get("license-file")
    else:
        item["license_metadata"] = "package not downloaded"
    packages.append(item)
compiler = json.loads((ROOT / "target/.rustc_info.json").read_text())
version = next((value["stdout"] for value in compiler["outputs"].values()
                if value.get("stdout", "").startswith("rustc ")), None)
result = {
    "kind": "generated artifact and resolved-package inventory; not verification evidence",
    "compiler_from_cargo_artifact_metadata": version,
    "execution_status": {
        "native_release": "built by Root through Click mutation; existing artifact retained",
        "native_recovery_test": "not executed: Click verify runner context binding was malformed",
        "wasm_release": "build executed; E0514: official rust std incompatible with Arch rustc metadata",
        "wasm_bindgen_glue": "not generated: WASM release prerequisite missing",
        "cloudflare_deployment": "not attempted",
    },
    "official_download_sources": [
        {"url": "https://static.rust-lang.org/dist/2026-09-03/rust-std-1.98.1-wasm32-unknown-unknown.tar.xz",
         "official_sha256": "cf1fcf68880d8b2b90128cede7416cfb82ad3584357ce9fb668f2b6244c26e55",
         "source": "https://static.rust-lang.org/dist/channel-rust-1.98.1.toml"},
        {"url": "https://github.com/wasm-bindgen/wasm-bindgen/releases/download/0.2.105/wasm-bindgen-0.2.105-x86_64-unknown-linux-musl.tar.gz",
         "official_sha256": "b391448c4926ac4b11425a6752484d85164e72489d97804461d5e868c643b88a",
         "source": "https://api.github.com/repos/wasm-bindgen/wasm-bindgen/releases/tags/0.2.105"},
    ],
    "artifacts": artifacts,
    "locked_packages": packages,
}
(ROOT / "ARTIFACTS.json").write_text(json.dumps(result, indent=2) + "\n")
print(f"Recorded {len(artifacts)} present files and {len(packages)} locked packages; no test executed.")
