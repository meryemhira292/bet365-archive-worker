#!/usr/bin/env python3
"""Confidential, authenticated transport for OAY hybrid exact candidates.

The repository is PUBLIC. Never commit raw candidate queues or key material.
The sender only needs the public key printed by the separate GitHub key job.
The receiver derives its private key from existing OAY_AES256_KEY_B64.
No 2026 results or odds are included in these packets.
"""
from __future__ import annotations
import argparse
import base64
import hashlib
import json
import os
import pathlib
import zipfile
import zlib
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

DOMAIN = b"OAY-HYBRID-INBOX-V1"
MAX_PLAINTEXT = 2_000_000
MAX_ROWS = 5000


def b64(x: bytes) -> str:
    return base64.b64encode(x).decode("ascii")


def unb64(x: str) -> bytes:
    return base64.b64decode(x, validate=True)


def recipient_secret():
    encoded = os.environ.get("OAY_AES256_KEY_B64")
    if not encoded:
        raise RuntimeError("Missing OAY_AES256_KEY_B64")
    master = unb64(encoded)
    if len(master) != 32:
        raise ValueError("Expected 32-byte master secret")
    private_bytes = HKDF(
        algorithm=hashes.SHA256(), length=32,
        salt=b"OAY-V9-PUBLIC-BRIDGE-20261011",
        info=b"static-x25519-key-v1",
    ).derive(master)
    return X25519PrivateKey.from_private_bytes(private_bytes)


def public_bytes(priv):
    return priv.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )


def rows_from_bytes(raw: bytes) -> list[dict]:
    if len(raw) > MAX_PLAINTEXT:
        raise ValueError("Chunk too large")
    rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
    if not (1 <= len(rows) <= MAX_ROWS):
        raise ValueError("Invalid row count")
    seen = set()
    for row in rows:
        cols = row.get("cols")
        direction = row.get("d")
        sig = row.get("sig")
        if (not isinstance(cols, list)
            or not 6 <= len(cols) <= 40
            or not all(isinstance(x, int) and not isinstance(x, bool) and 0 <= x < 256 for x in cols)
            or cols != sorted(set(cols))
            or type(direction) is not int or not 0 <= direction <= 6
            or not isinstance(sig, str) or len(sig) != 64
            or any(c not in "0123456789abcdef" for c in sig)
            or sig in seen):
            raise ValueError("Malformed candidate, direction or duplicate signature")
        seen.add(sig)
    return rows


def seal(raw: bytes, recipient_public: bytes, source_sha: str, index: int) -> dict:
    rows_from_bytes(raw)
    eph = X25519PrivateKey.generate()
    peer = X25519PublicKey.from_public_bytes(recipient_public)
    salt = os.urandom(16)
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=salt,
               info=DOMAIN).derive(eph.exchange(peer))
    nonce = os.urandom(12)
    ciphertext = AESGCM(key).encrypt(nonce, zlib.compress(raw, level=9), DOMAIN)
    return {
        "schema": "oay-hybrid-inbox-v1",
        "source_sha256": source_sha,
        "chunk_index": index,
        "rows": len(rows_from_bytes(raw)),
        "raw_sha256": hashlib.sha256(raw).hexdigest(),
        "ephemeral_public": b64(public_bytes(eph)),
        "salt": b64(salt),
        "nonce": b64(nonce),
        "ciphertext": b64(ciphertext),
    }


def open_packet(packet: dict, priv) -> bytes:
    if packet.get("schema") != "oay-hybrid-inbox-v1":
        raise ValueError("Unsupported transport schema")
    sender = X25519PublicKey.from_public_bytes(unb64(packet["ephemeral_public"]))
    salt = unb64(packet["salt"])
    nonce = unb64(packet["nonce"])
    if len(salt) != 16 or len(nonce) != 12:
        raise ValueError("Incorrect nonce or salt size")
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=salt,
               info=DOMAIN).derive(priv.exchange(sender))
    compressed = AESGCM(key).decrypt(nonce, unb64(packet["ciphertext"]), DOMAIN)
    decomp = zlib.decompressobj()
    raw = decomp.decompress(compressed, MAX_PLAINTEXT + 1)
    if (len(raw) > MAX_PLAINTEXT or decomp.unconsumed_tail
        or not decomp.eof or decomp.unused_data):
        raise ValueError("Packet decompression limit exceeded or trailing data")
    rows = rows_from_bytes(raw)
    if len(rows) != packet["rows"] or hashlib.sha256(raw).hexdigest() != packet["raw_sha256"]:
        raise ValueError("Packet integrity mismatch")
    return raw


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("public-key")
    sp = sub.add_parser("seal")
    sp.add_argument("--gate", type=pathlib.Path, required=True)
    sp.add_argument("--public-key", required=True)
    sp.add_argument("--outdir", type=pathlib.Path, required=True)
    sp.add_argument("--rows-per-chunk", type=int, default=2000)
    op = sub.add_parser("open")
    op.add_argument("--packet", type=pathlib.Path, required=True)
    op.add_argument("--out", type=pathlib.Path, required=True)
    args = parser.parse_args()
    if args.action == "public-key":
        print("OAY_BRIDGE_PUBKEY_B64=" + b64(public_bytes(recipient_secret())))
        return
    if args.action == "seal":
        if not 1 <= args.rows_per_chunk <= MAX_ROWS:
            raise ValueError("Bad rows-per-chunk")
        with zipfile.ZipFile(args.gate) as z:
            manifest = json.loads(z.read("MANIFEST_SHA256.json"))
            raw = z.read("EXACT_QUEUE.jsonl")
            if hashlib.sha256(raw).hexdigest() != manifest["EXACT_QUEUE.jsonl"]:
                raise ValueError("Source Gate manifest mismatch")
        lines = raw.splitlines(keepends=True)
        base_sha = hashlib.sha256(args.gate.read_bytes()).hexdigest()
        public_key = unb64(args.public_key)
        args.outdir.mkdir(parents=True, exist_ok=True)
        for i in range(0, len(lines), args.rows_per_chunk):
            body = b"".join(lines[i:i+args.rows_per_chunk])
            packet = seal(body, public_key, base_sha, i // args.rows_per_chunk)
            name = f"{base_sha[:16]}_{i//args.rows_per_chunk:05d}.json"
            path = args.outdir / name
            if path.exists():
                raise FileExistsError(f"Refusing overwrite: {path}")
            path.write_text(json.dumps(packet, separators=(",", ":")) + "\n")
            print(f"SEALED {name}: {packet['rows']} candidates")
        return
    packet = json.loads(args.packet.read_text())
    raw = open_packet(packet, recipient_secret())
    if args.out.exists():
        raise FileExistsError("Refusing plaintext overwrite")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_bytes(raw)
    print("DECRYPTED_AND_VERIFIED", len(rows_from_bytes(raw)))


if __name__ == "__main__":
    main()
