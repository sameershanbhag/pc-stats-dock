#!/usr/bin/env python3
"""Certificates through the App Store Connect API, with an API key instead of a browser.

  python3 asc-cert.py list   --key AuthKey_X.p8 --key-id X --issuer <issuer uuid>
  python3 asc-cert.py create --key ... --key-id ... --issuer ... --csr request.csr --out cert.cer [--type DEVELOPER_ID_APPLICATION]

The JWT is signed with openssl (ES256), so nothing needs installing.
"""
import argparse
import base64
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

API = "https://api.appstoreconnect.apple.com"


def b64url(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def der_to_raw(sig):
    """ECDSA signature DER -> r||s, 32 bytes each (what JWT ES256 wants)."""
    i = 2 + (1 if sig[1] & 0x80 else 0)

    def integer(i):
        assert sig[i] == 0x02
        n = sig[i + 1]
        return sig[i + 2:i + 2 + n], i + 2 + n
    r, i = integer(i)
    s, i = integer(i)
    return r.lstrip(b"\x00").rjust(32, b"\x00") + s.lstrip(b"\x00").rjust(32, b"\x00")


def token(key, key_id, issuer):
    now = int(time.time())
    head = b64url(json.dumps({"alg": "ES256", "kid": key_id, "typ": "JWT"}, separators=(",", ":")).encode())
    body = b64url(json.dumps({"iss": issuer, "iat": now, "exp": now + 900, "aud": "appstoreconnect-v1"}, separators=(",", ":")).encode())
    signing = f"{head}.{body}"
    with tempfile.NamedTemporaryFile(delete=False) as f:
        f.write(signing.encode())
    try:
        der = subprocess.run(["/usr/bin/openssl", "dgst", "-sha256", "-sign", key, f.name], capture_output=True, check=True).stdout
    finally:
        os.unlink(f.name)
    return f"{signing}.{b64url(der_to_raw(der))}"


def call(tok, method, path, body=None):
    req = urllib.request.Request(API + path, data=json.dumps(body).encode() if body else None, method=method,
                                 headers={"Authorization": "Bearer " + tok, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("verb", choices=["list", "create"])
    ap.add_argument("--key", required=True)
    ap.add_argument("--key-id", required=True)
    ap.add_argument("--issuer", required=True)
    ap.add_argument("--csr")
    ap.add_argument("--out")
    ap.add_argument("--type", default="DEVELOPER_ID_APPLICATION")
    a = ap.parse_args()
    tok = token(a.key, a.key_id, a.issuer)
    if a.verb == "list":
        code, r = call(tok, "GET", "/v1/certificates?limit=200")
        if code != 200:
            print("error", code, json.dumps(r)[:400])
            return 1
        for c in r.get("data", []):
            at = c["attributes"]
            print(f'{c["id"]:>12}  {at.get("certificateType"):<28} {at.get("displayName")!s:<40} exp {str(at.get("expirationDate"))[:10]}  {at.get("name")}')
        print(f"{len(r.get('data', []))} certificates")
        return 0
    if not a.csr or not a.out:
        ap.error("create needs --csr and --out")
    pem = open(a.csr).read()
    body = {"data": {"type": "certificates", "attributes": {"certificateType": a.type, "csrContent": pem}}}
    code, r = call(tok, "POST", "/v1/certificates", body)
    if code not in (200, 201):
        inner = "".join(l for l in pem.splitlines() if not l.startswith("-----"))
        body["data"]["attributes"]["csrContent"] = inner
        code, r = call(tok, "POST", "/v1/certificates", body)
    if code not in (200, 201):
        print("error", code, json.dumps(r)[:600])
        return 1
    at = r["data"]["attributes"]
    with open(a.out, "wb") as f:
        f.write(base64.b64decode(at["certificateContent"]))
    print(f'created {at.get("certificateType")} "{at.get("name")}" ({r["data"]["id"]}), expires {str(at.get("expirationDate"))[:10]} -> {a.out}')
    return 0


if __name__ == "__main__":
    sys.exit(main())
