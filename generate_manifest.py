#!/usr/bin/env python3
"""
IT Aman -- Update Manifest Generator v3.0
==========================================
Generates update_manifest.json with:
  - SHA256 hashes of all downloadable files
  - Ed25519 signature for integrity verification

The private key is NEVER stored in the repository.  It is kept offline by the
developer and loaded from a file when generating a new manifest.

Usage:
  python3 generate_manifest.py [version] [repo_root] [--key /path/to/private_key.pem]

Example:
  python3 generate_manifest.py 3.1 . --key ~/.it-aman/ed25519_private.pem

The generated manifest should be committed to the repo alongside the source
files it references.  daemon.py verifies the signature using the embedded
public key — an attacker cannot forge a valid signature without the private
key, even if they have full read access to the public repository.
"""

import argparse
import base64
import hashlib
import json
import os
import sys

# ---------------------------------------------------------------------------
# Files to include in the manifest (remote_path -> local_path)
# ---------------------------------------------------------------------------
MANIFEST_FILES = {
    "src/daemon.py": "src/daemon.py",
    "src/gui.py": "src/gui.py",
    "data.json": "data.json",
    "version.json": "version.json",
}


def compute_sha256(file_path: str) -> str:
    """Compute SHA256 hash of a file."""
    h = hashlib.sha256()
    try:
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
        return h.hexdigest()
    except FileNotFoundError:
        return ""


def sign_manifest(manifest: dict, private_key_path: str) -> str:
    """
    Sign the manifest with an Ed25519 private key.

    Returns the base64-encoded signature.
    """
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.serialization import (
            Encoding,
            NoEncryption,
            PrivateFormat,
            load_pem_private_key,
        )
    except ImportError:
        print("ERROR: 'cryptography' package is required for signing.")
        print("  Install with: pip3 install cryptography")
        sys.exit(1)

    # Load private key
    try:
        with open(private_key_path, "rb") as f:
            private_key = load_pem_private_key(f.read(), password=None)
    except FileNotFoundError:
        print(f"ERROR: Private key file not found: {private_key_path}")
        print("  Generate one with: python3 generate_keypair.py")
        sys.exit(1)
    except Exception as exc:
        print(f"ERROR: Failed to load private key: {exc}")
        sys.exit(1)

    # Create canonical JSON of the manifest (without the signature field)
    manifest_json = json.dumps(manifest, sort_keys=True, ensure_ascii=False)
    message_bytes = manifest_json.encode("utf-8")

    # Sign with Ed25519
    signature = private_key.sign(message_bytes)
    signature_b64 = base64.b64encode(signature).decode("ascii")

    # Also display the public key for reference (so the developer can verify
    # it matches what's embedded in daemon.py)
    from cryptography.hazmat.primitives.serialization import PublicFormat
    pub_key_b64 = base64.b64encode(
        private_key.public_key().public_bytes(
            encoding=Encoding.Raw,
            format=PublicFormat.Raw,
        )
    ).decode("ascii")

    print(f"  Public key (for daemon.py): {pub_key_b64}")
    return signature_b64


def generate_manifest(version: str, repo_root: str, private_key_path: str) -> dict:
    """Generate the update manifest with SHA256 hashes and Ed25519 signature."""
    manifest = {
        "version": version,
        "files": {},
    }

    # Compute hashes for each file
    for remote_path, local_path in MANIFEST_FILES.items():
        full_path = os.path.join(repo_root, local_path)
        sha256 = compute_sha256(full_path)
        if sha256:
            manifest["files"][remote_path] = sha256
            print(f"  {remote_path}: {sha256[:16]}...")
        else:
            print(f"  WARNING: {full_path} not found, skipping")

    # Add changelog BEFORE signing (so it's covered by the signature)
    manifest["changelog"] = f"v{version}: Update release"

    # Sign the manifest with Ed25519
    print("\nSigning manifest with Ed25519...")
    signature = sign_manifest(manifest, private_key_path)
    manifest["signature"] = signature

    return manifest


def main():
    parser = argparse.ArgumentParser(
        description="Generate IT Aman update manifest with Ed25519 signature"
    )
    parser.add_argument(
        "version", nargs="?", default="3.0",
        help="Version string (default: 3.0)"
    )
    parser.add_argument(
        "repo_root", nargs="?", default=".",
        help="Repository root directory (default: current directory)"
    )
    parser.add_argument(
        "--key", "-k",
        default=os.path.expanduser("~/.it-aman/ed25519_private.pem"),
        help="Path to Ed25519 private key PEM file "
             "(default: ~/.it-aman/ed25519_private.pem)"
    )
    args = parser.parse_args()

    print(f"Generating update manifest for v{args.version}...")
    print(f"  Repository root: {os.path.abspath(args.repo_root)}")
    print(f"  Private key: {args.key}")
    print()

    manifest = generate_manifest(args.version, args.repo_root, args.key)

    output_path = os.path.join(args.repo_root, "update_manifest.json")
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    print(f"\nManifest written to: {output_path}")
    print(f"Version: {manifest['version']}")
    print(f"Files: {len(manifest['files'])}")
    print(f"Signature: {manifest['signature'][:32]}...")
    print()
    print("IMPORTANT: Commit this manifest to the repository alongside the")
    print("           updated source files. The signature ensures that end-")
    print("           users can verify the update came from you.")


if __name__ == "__main__":
    main()
