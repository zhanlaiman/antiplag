import io
import json
from urllib.error import HTTPError, URLError
import unittest
from unittest.mock import Mock

from plagiarism.auth_provider import AccessError, AuthConfig, EmailProvider, NoRedirect


def config(**changes):
    values = {"origin": "https://app.example.org", "supabase_url": "https://project-name.supabase.co",
              "supabase_key": "server-only-key", "captcha_site_key": "configured-site-key",
              "captcha_secret": "configured-secret-key", **changes}
    return AuthConfig(**values)


def response(value):
    return io.BytesIO(json.dumps(value).encode())


class AuthProviderTests(unittest.TestCase):
    def test_public_config_is_https_and_fails_closed_without_secret_or_real_captcha(self):
        for changes in ({"origin": "http://public.example.org"}, {"origin": "https://app.example.org/path"},
                        {"supabase_url": "https://attacker.example"}, {"supabase_url": "https://project-name.supabase.co.attacker.example"},
                        {"supabase_url": "http://project-name.supabase.co"}, {"supabase_key": ""},
                        {"captcha_secret": "1x0000000000000000000000000000000AA"}):
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    config(**changes)
        self.assertFalse(config(origin="http://127.0.0.1:8766").secure_cookies)
        self.assertTrue(config().secure_cookies)
        self.assertNotIn("server-only-key", repr(config()))
        self.assertNotIn("configured-secret-key", repr(config()))

    def test_captcha_is_verified_once_on_exact_hostname_and_signin_action(self):
        provider = EmailProvider(config())
        provider.opener = Mock()
        provider.opener.open.side_effect = [response({"success": True, "hostname": "app.example.org", "action": "signin"}), response({})]
        provider.request_code("Editor@Example.org", "captcha-response")
        requests = [call.args[0] for call in provider.opener.open.call_args_list]
        self.assertEqual(requests[0].full_url, "https://challenges.cloudflare.com/turnstile/v0/siteverify")
        self.assertNotIn("Apikey", requests[0].headers)
        captcha_body = json.loads(requests[0].data)
        self.assertEqual(captcha_body, {"secret": "configured-secret-key", "response": "captcha-response"})
        self.assertEqual(json.loads(requests[1].data), {"email": "editor@example.org", "create_user": True})
        self.assertNotIn(b"captcha-response", requests[1].data)

    def test_invalid_captcha_never_sends_email_or_forwards_provider_error(self):
        for result in ({"success": False}, {"success": True, "hostname": "attacker.example", "action": "signin"},
                       {"success": True, "hostname": "app.example.org", "action": "other"}):
            provider = EmailProvider(config())
            provider.opener = Mock()
            provider.opener.open.return_value = response(result)
            with self.assertRaises(AccessError) as raised:
                provider.request_code("editor@example.org", "captcha-response")
            self.assertEqual(raised.exception.code, "captcha_invalid")
            self.assertEqual(provider.opener.open.call_count, 1)

    def test_verify_uses_trusted_user_endpoint_and_never_returns_bearer_token(self):
        provider = EmailProvider(config())
        provider.opener = Mock()
        token = "private-bearer-token-123456789"
        provider.opener.open.side_effect = [response({"access_token": token, "user": {"id": "forged"}}), response({
            "id": "12345678-1234-1234-1234-123456789abc", "email": "editor@example.org",
            "email_confirmed_at": "2026-11-11T12:00:00Z", "user_metadata": {"plan": "paid"}})]
        identity = provider.verify_code("editor@example.org", "123456")
        self.assertEqual(identity["subject"], "12345678-1234-1234-1234-123456789abc")
        self.assertIs(identity["email_verified"], True)
        self.assertNotIn(token, json.dumps(identity))
        request = provider.opener.open.call_args_list[1].args[0]
        self.assertTrue(request.full_url.endswith("/auth/v1/user"))
        self.assertEqual(request.get_header("Authorization"), "Bearer " + token)

    def test_user_email_verification_is_not_taken_from_mutable_metadata(self):
        for changes in ({"email_confirmed_at": None, "user_metadata": {"email_verified": True}},
                        {"is_anonymous": True}, {"email": "other@example.org"}):
            provider = EmailProvider(config())
            provider.opener = Mock()
            provider.opener.open.side_effect = [response({"access_token": "private-bearer-token-123456789"}), response({
                "id": "12345678-1234-1234-1234-123456789abc", "email": "editor@example.org",
                "email_confirmed_at": "2026-11-11T12:00:00Z", **changes})]
            with self.assertRaises(AccessError) as raised:
                provider.verify_code("editor@example.org", "123456")
            self.assertEqual(raised.exception.code, "email_not_verified")

    def test_provider_failures_are_sanitized_closed_and_bounded(self):
        for status, expected in ((302, "auth_unavailable"), (401, "invalid_login"), (429, "auth_rate_limited"), (500, "auth_unavailable")):
            provider = EmailProvider(config())
            provider.opener = Mock()
            secret = io.BytesIO(b"private-token-and-user-details")
            provider.opener.open.side_effect = HTTPError("https://project-name.supabase.co", status, "failed", {}, secret)
            with self.assertRaises(AccessError) as raised:
                provider._json("https://project-name.supabase.co/auth/v1/otp", {})
            self.assertEqual(raised.exception.code, expected)
            self.assertNotIn("private-token", str(raised.exception))
            self.assertTrue(secret.closed)
        provider.opener.open.side_effect = URLError("network secret")
        with self.assertRaises(AccessError):
            provider._json("https://project-name.supabase.co/auth/v1/otp", {})

    def test_provider_redirect_and_oversized_response_are_rejected(self):
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, "", {}, "https://attacker.example"))
        provider = EmailProvider(config())
        provider.opener = Mock()
        provider.opener.open.return_value = io.BytesIO(b"x" * 131073)
        with self.assertRaises(AccessError) as raised:
            provider._json("https://project-name.supabase.co/auth/v1/otp", {})
        self.assertEqual(raised.exception.code, "auth_unavailable")


if __name__ == "__main__":
    unittest.main()
