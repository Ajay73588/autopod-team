"""HMAC-SHA256 webhook signature verification tests (item 6.2)."""

import hmac
import hashlib
import json

from webhook_handler import verify_github_signature


SECRET = "test-webhook-secret"


def _sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_valid_signature_is_accepted():
    body = json.dumps({"test": True}).encode()
    signature = _sign(body)
    assert verify_github_signature(SECRET, body, signature) is True


def test_invalid_signature_is_rejected():
    body = json.dumps({"test": True}).encode()
    bad_signature = _sign(body, secret="wrong-secret")
    assert verify_github_signature(SECRET, body, bad_signature) is False


def test_missing_signature_is_rejected():
    body = json.dumps({"test": True}).encode()
    assert verify_github_signature(SECRET, body, "") is False
    assert verify_github_signature(SECRET, body, None) is False


def test_malformed_signature_header_is_rejected():
    body = json.dumps({"test": True}).encode()
    assert verify_github_signature(SECRET, body, "not-a-real-signature") is False
    assert verify_github_signature(SECRET, body, "sha1=deadbeef") is False


def test_signature_is_tied_to_exact_body_bytes():
    """A signature computed over one payload must not validate a
    different payload -- this is what actually prevents a tampered
    request from passing verification."""
    body_a = json.dumps({"test": True, "n": 1}).encode()
    body_b = json.dumps({"test": True, "n": 2}).encode()
    signature_for_a = _sign(body_a)
    assert verify_github_signature(SECRET, body_a, signature_for_a) is True
    assert verify_github_signature(SECRET, body_b, signature_for_a) is False
