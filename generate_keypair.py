#!/usr/bin/env python3
"""
IT Aman -- Ed25519 Key Pair Generator
======================================
One-time tool to generate an Ed25519 key pair for signing update manifests.

Usage:
  python3 generate_keypair.py [--output-dir ~/.it-aman]

This creates two files:
  - ed25519_private.pem  (KEEP SECRET — never commit to the repository!)
  - ed25519_public.pem   (for reference; the base64 public key goes in daemon.py)

After generating:
  1. Copy the printed PUBLIC KEY (base64) into daemon.py's
     _MANIFEST_PUBLIC_KEY_B64 constant.
  2. Store the private key file securely (e.g. on an encrypted USB drive
     or a password manager).  You will need it every time you run
     generate_manifest.py to publish a new release.
  3. NEVER commit the private key to the repository.
"""

import argparse
import base64
import os
import sys


def main():
    parser = argparse.ArgumentParser(
        description="Generate Ed25519 key pair for IT Aman manifest signing"
    )
    parser.add_argument(
        "--output-dir", "-o",
        default=os.path.expanduser("~/.it-aman"),
        help="Directory to save key files (default: ~/.it-aman)"
    )
    args = parser.parse_args()

    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.serialization import (
            Encoding,
            NoEncryption,
            PrivateFormat,
            PublicFormat,
        )
    except ImportError:
        print("ERROR: 'cryptography' package is required.")
        print("  Install with: pip3 install cryptography")
        sys.exit(1)

    # Generate key pair
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()

    # Serialize private key (PEM, no password)
    private_pem = private_key.private_bytes(
        encoding=Encoding.PEM,
        format=PrivateFormat.PKCS8,
        encryption_algorithm=NoEncryption(),
    )

    # Serialize public key (PEM, for reference)
    public_pem = public_key.public_bytes(
        encoding=Encoding.PEM,
        format=PublicFormat.SubjectPublicKeyInfo,
    )

    # Raw public key bytes (for embedding in daemon.py)
    pub_raw = public_key.public_bytes(
        encoding=Encoding.Raw,
        format=PublicFormat.Raw,
    )
    pub_b64 = base64.b64encode(pub_raw).decode("ascii")

    # Create output directory
    os.makedirs(args.output_dir, exist_ok=True)

    # Set restrictive permissions on the directory
    os.chmod(args.output_dir, 0o700)

    # PROTECTION: Do NOT overwrite existing key pair
    priv_path = os.path.join(args.output_dir, "ed25519_private.pem")
    pub_path = os.path.join(args.output_dir, "ed25519_public.pem")

    if os.path.exists(priv_path):
        print("=" * 70)
        print("  WARNING: Private key already exists!")
        print("=" * 70)
        print(f"  Existing key: {priv_path}")
        print()
        print("  Generating a NEW key pair would INVALIDATE all existing")
        print("  signatures and break auto-update on all client machines.")
        print()
        print("  If you REALLY want to regenerate:")
        print(f"    1. rm {priv_path}")
        print(f"    2. rm {pub_path}")
        print("    3. Run this script again")
        print()
        print("  ABORTING — no changes made.")
        print("=" * 70)
        sys.exit(1)

    # Write private key
    with open(priv_path, "wb") as f:
        f.write(private_pem)
    os.chmod(priv_path, 0o600)  # Owner read/write only

    # Write public key
    with open(pub_path, "wb") as f:
        f.write(public_pem)
    os.chmod(pub_path, 0o644)

    print("=" * 70)
    print("  IT Aman — Ed25519 Key Pair Generated")
    print("=" * 70)
    print()
    print(f"  Private key: {priv_path}")
    print(f"  Public key:  {pub_path}")
    print()
    print("-" * 70)
    print("  PUBLIC KEY (base64) — paste this into daemon.py:")
    print("-" * 70)
    print(f"  {pub_b64}")
    print("-" * 70)
    print()
    print("  NEXT STEPS:")
    print()
    print("  1. Copy the PUBLIC KEY above into daemon.py:")
    print("     _MANIFEST_PUBLIC_KEY_B64 = (")
    print(f"         \"{pub_b64}\"")
    print("     )")
    print()
    print("  2. Store the private key file SECURELY:")
    print(f"     {priv_path}")
    print("     - NEVER commit it to the repository")
    print("     - Keep it on an encrypted drive or in a password manager")
    print()
    print("  3. When publishing a release, sign the manifest:")
    print(f"     python3 generate_manifest.py 3.1 . --key {priv_path}")
    print()
    print("=" * 70)


if __name__ == "__main__":
    main()
