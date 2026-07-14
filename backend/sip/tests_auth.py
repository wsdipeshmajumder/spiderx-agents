"""Digest-auth checks (run: python -m backend.sip.tests_auth).

Simulates a UA computing an Authorization header for known credentials, then
verifies our server-side recompute accepts the right one and rejects wrong
password / stale nonce. Covers both qop=auth (Grandstream) and classic.
"""
from . import auth


def check(name, cond):
    if not cond:
        raise AssertionError("FAIL: " + name)
    print("  ok:", name)


CREDS = dict(username="tata_ai", password="s3cr3t-pass", realm="spiderx.ai")
URI = "sip:agent-5@sip.spiderx.ai"


def _client_auth(nonce, *, method="REGISTER", qop=None, nc=None, cnonce=None, password=None):
    resp = auth.expected_response(
        username=CREDS["username"], password=password or CREDS["password"],
        realm=CREDS["realm"], nonce=nonce, method=method, uri=URI,
        qop=qop, nc=nc, cnonce=cnonce)
    parts = [f'username="{CREDS["username"]}"', f'realm="{CREDS["realm"]}"',
             f'nonce="{nonce}"', f'uri="{URI}"', f'response="{resp}"', "algorithm=MD5"]
    if qop:
        parts += [f"qop={qop}", f"nc={nc}", f'cnonce="{cnonce}"']
    return "Digest " + ", ".join(parts)


def test_parse():
    print("parse")
    p = auth.parse_authorization('Digest username="u", realm="r", nonce="abc", qop=auth, nc=00000001')
    check("strips Digest + quotes", p["username"] == "u" and p["realm"] == "r" and p["nonce"] == "abc")
    check("keeps unquoted params", p["qop"] == "auth" and p["nc"] == "00000001")


def test_challenge():
    print("challenge")
    nonce, name, value = auth.make_challenge("spiderx.ai")
    check("WWW-Authenticate by default", name == "WWW-Authenticate")
    check("carries realm + nonce + qop", 'realm="spiderx.ai"' in value and f'nonce="{nonce}"' in value and 'qop="auth"' in value)
    _, pname, _ = auth.make_challenge("spiderx.ai", proxy=True)
    check("proxy variant is Proxy-Authenticate", pname == "Proxy-Authenticate")


def test_verify_classic():
    print("verify (no qop)")
    nonce = "deadbeefnonce"
    hdr = _client_auth(nonce)
    ok, user = auth.verify(hdr, method="REGISTER", realm=CREDS["realm"],
                           password=CREDS["password"], expected_nonce=nonce)
    check("accepts correct credentials", ok and user == "tata_ai")
    bad = _client_auth(nonce, password="wrong")
    ok2, _ = auth.verify(bad, method="REGISTER", realm=CREDS["realm"], password=CREDS["password"], expected_nonce=nonce)
    check("rejects wrong password", not ok2)
    ok3, _ = auth.verify(hdr, method="REGISTER", realm=CREDS["realm"], password=CREDS["password"], expected_nonce="other-nonce")
    check("rejects stale/foreign nonce", not ok3)


def test_verify_qop():
    print("verify (qop=auth, Grandstream-style)")
    nonce = "grandstreamnonce123"
    hdr = _client_auth(nonce, method="INVITE", qop="auth", nc="00000001", cnonce="0a4f113b")
    ok, user = auth.verify(hdr, method="INVITE", realm=CREDS["realm"],
                           password=CREDS["password"], expected_nonce=nonce)
    check("accepts correct qop=auth response", ok and user == "tata_ai")
    # method must be bound into HA2 — an INVITE response must NOT verify as REGISTER
    ok2, _ = auth.verify(hdr, method="REGISTER", realm=CREDS["realm"], password=CREDS["password"], expected_nonce=nonce)
    check("method is bound (INVITE response fails as REGISTER)", not ok2)


def test_ha1():
    print("HA1 (stored-hash) verify")
    nonce = "n123"
    import hashlib
    ha1 = hashlib.md5(f'{CREDS["username"]}:{CREDS["realm"]}:{CREDS["password"]}'.encode()).hexdigest()
    hdr = _client_auth(nonce)
    ok, _ = auth.verify(hdr, method="REGISTER", realm=CREDS["realm"], ha1=ha1, expected_nonce=nonce)
    check("verifies against stored HA1 without plaintext password", ok)


if __name__ == "__main__":
    test_parse(); test_challenge(); test_verify_classic(); test_verify_qop(); test_ha1()
    print("\nALL DIGEST-AUTH CHECKS PASSED ✅")
