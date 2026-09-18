"""Account policy links open only fixed public pages, never arbitrary URLs."""
from types import SimpleNamespace
from unittest.mock import patch
from hub.webui.screens.competitive import _account_policy


def test_account_policies_open_fixed_public_documents():
    panel = SimpleNamespace(post=lambda action: action())
    with patch("webbrowser.open") as opened:
        for page in ("privacy", "terms"):
            _account_policy(panel, page)
            opened.assert_called_with("https://lightsoutranked.com/" + page)
        for page in ("", "https://example.test", "../account", "privacy?token=private"):
            _account_policy(panel, page)
        assert opened.call_count == 2
