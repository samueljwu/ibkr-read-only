from urllib.request import Request

from app.flex.flex_import import _RejectRedirects


def test_flex_token_is_not_forwarded_to_redirect_target():
    original = Request("https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService/SendRequest?t=fake-token")
    redirect = _RejectRedirects().redirect_request(
        original, None, 302, "Found", {}, "https://example.invalid/collect"
    )
    assert redirect is None
