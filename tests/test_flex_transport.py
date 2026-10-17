from urllib.request import HTTPSHandler, Request
from urllib.error import URLError
from ssl import CERT_REQUIRED, SSLCertVerificationError

import pytest

from app.flex import flex_import
from app.flex.flex_import import _RejectRedirects


def test_flex_https_verifies_certificates_and_hostnames():
    handler = next(
        item for item in flex_import._opener.handlers if isinstance(item, HTTPSHandler)
    )
    assert handler._context.verify_mode == CERT_REQUIRED
    assert handler._context.check_hostname
    assert handler._context.get_ca_certs()


def test_flex_token_is_not_forwarded_to_redirect_target():
    original = Request("https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService/SendRequest?t=fake-token")
    redirect = _RejectRedirects().redirect_request(
        original, None, 302, "Found", {}, "https://example.invalid/collect"
    )
    assert redirect is None


def test_worker_credential_failure_has_safe_diagnostic(monkeypatch):
    def denied(_account):
        raise RuntimeError("synthetic-secret https://example.invalid/?t=synthetic-secret")

    monkeypatch.setattr(flex_import, "load_flex_token", denied)
    with pytest.raises(flex_import.FlexEnvironmentError) as result:
        flex_import.resolve_flex_token("SYNTHETIC")
    message = flex_import.safe_flex_error(result.value)
    assert "credential store" in message
    assert "synthetic-secret" not in message and "https://" not in message


def test_tls_failure_has_safe_diagnostic(monkeypatch):
    class FailingOpener:
        def open(self, *_args, **_kwargs):
            raise URLError(SSLCertVerificationError(
                "synthetic-secret https://example.invalid/?t=synthetic-secret"))

    monkeypatch.setattr(flex_import, "_opener", FailingOpener())
    with pytest.raises(flex_import.FlexEnvironmentError) as result:
        flex_import.fetch_flex("synthetic-query", "synthetic-secret")
    message = flex_import.safe_flex_error(result.value)
    assert "certificate verification" in message
    assert "synthetic-secret" not in message and "https://" not in message
