#!/usr/bin/env python3
"""Verify a Parsica Spirit store salt against its ledger fingerprint - before trusting it.

The store's promotion ledger records a non-secret fingerprint (`salt_fp`) of the salt its
record identities were minted under. If the salt file is ever lost and restored from a backup,
check the restored file against that fingerprint FIRST: promoting under the wrong salt would
re-key every record and fork the store (the promote preflight refuses this, loudly).

Usage:
    python verify_salt.py <path-to-.salt> [expected-fingerprint]

The expected fingerprint lives in the store at
    <install-root>/<scope>/.parsica_router/promotion_sync.json   ->  "salt_fp"
With no expected fingerprint argument, the computed fingerprint is printed for manual
comparison. Stdlib-only on purpose: this must run on a bare recovery machine with nothing
but Python installed. Reads the salt file; writes nothing.
"""
import hashlib
import hmac
import sys

# must match parsica_spirit.promote._SALT_FP_LABEL / salt_fingerprint (HMAC-SHA256, 12 hex)
LABEL = b"\x00parsica-salt-fingerprint-v1\x00"


def fingerprint(salt_path):
    with open(salt_path, "rb") as f:
        raw = f.read().strip()
    salt = bytes.fromhex(raw.decode("ascii"))
    if len(salt) < 16:
        raise ValueError("salt file holds fewer than 16 bytes - not a Parsica salt")
    return hmac.new(salt, LABEL, hashlib.sha256).hexdigest()[:12]


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__.strip())
        sys.exit(2)
    fp = fingerprint(sys.argv[1])
    print("salt file   :", sys.argv[1])
    print("fingerprint :", fp)
    if len(sys.argv) > 2:
        want = sys.argv[2].strip()
        print("expected    :", want)
        ok = fp == want
        print("VERDICT     :", "MATCH - safe to trust this salt"
              if ok else "MISMATCH - DO NOT trust this salt")
        sys.exit(0 if ok else 1)
    print("(compare against the ledger's salt_fp in "
          "<scope>/.parsica_router/promotion_sync.json)")
