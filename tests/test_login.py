import unittest
import io
import base64
import json
from contextlib import redirect_stdout
from unittest.mock import MagicMock
from urllib.error import HTTPError

from check_login import diagnostic_summary, login_response_summary, verify_bearer, token_user_id, session_user_id


class LoginDiagnosticsTests(unittest.TestCase):
    def test_refused_bearer_retries_same_profile_using_cookie(self):
        opener = MagicMock()
        uid = "12345678-1234-5678-1234-567812345678"
        responses = []
        for body in [json.dumps({"id": uid, "email": "me@example.com"}).encode(), b'[]']:
            response = MagicMock()
            response.__enter__.return_value.read.return_value = body
            responses.append(response)
        opener.open.side_effect = [HTTPError("https://prod.fuelin.io", 403, "Forbidden", {}, io.BytesIO()), *responses]
        with redirect_stdout(io.StringIO()):
            self.assertTrue(verify_bearer(opener, "secret-token", "me@example.com", uid))
        requests = [call.args[0] for call in opener.open.call_args_list]
        self.assertEqual(requests[0].full_url, requests[1].full_url)
        self.assertIsNone(requests[1].get_header("Authorization"))
        self.assertEqual(requests[1].get_header("Cookie"), "__Secure-authjs.session-token=secret-token")
        self.assertTrue(all(request.get_method() == "GET" for request in requests))

    def test_token_id_hint_and_opaque_tokens(self):
        uid = "12345678-1234-5678-1234-567812345678"
        for claims in [{"sub": uid}, {"userId": uid}, {"user": {"id": uid}}]:
            encoded = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
            self.assertEqual(token_user_id("header." + encoded + ".signature"), uid)
        for token in ["opaque", "a.b.c.d.e", "a.invalid.c"]:
            self.assertIsNone(token_user_id(token))

    def test_opaque_token_without_id_uses_session_not_user_list(self):
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value.read.return_value = b'{}'
        with redirect_stdout(io.StringIO()):
            self.assertFalse(verify_bearer(opener, "opaque", "me@example.com"))
        self.assertEqual(opener.open.call_count, 1)
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://prod.fuelin.io/core/auth/session")
        self.assertEqual(request.get_method(), "GET")

    def test_session_identity_requires_matching_email(self):
        opener = MagicMock()
        uid = "12345678-1234-5678-1234-567812345678"
        for email, expected in [("me@example.com", uid), ("other@example.com", None)]:
            opener.open.return_value.__enter__.return_value.read.return_value = json.dumps({"user": {"email": email, "id": uid}}).encode()
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(session_user_id(opener, "secret-token", "me@example.com"), expected)
            self.assertNotIn(uid, output.getvalue())
            self.assertNotIn("secret-token", output.getvalue())

    def test_bearer_probe_reads_only_matching_account(self):
        opener = MagicMock()
        responses = []
        for body in [b'{"email":"me@example.com","id":"12345678-1234-5678-1234-567812345678","timezone":"Australia/Sydney"}', b'[]']:
            response = MagicMock()
            response.__enter__.return_value.read.return_value = body
            responses.append(response)
        opener.open.side_effect = responses
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertTrue(verify_bearer(opener, "private-token", "me@example.com", "12345678-1234-5678-1234-567812345678"))
        requests = [call.args[0] for call in opener.open.call_args_list]
        self.assertTrue(all(request.get_method() == "GET" for request in requests))
        self.assertIn("/users/12345678-1234-5678-1234-567812345678/days/", requests[1].full_url)
        self.assertEqual(requests[1].get_header("Authorization"), "Bearer private-token")
        self.assertNotIn("private-token", output.getvalue())
        self.assertNotIn("me@example.com", output.getvalue())

    def test_probe_stops_when_account_does_not_match(self):
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value.read.return_value = b'[{"email":"someone@else.com","id":"other"}]'
        with redirect_stdout(io.StringIO()):
            self.assertFalse(verify_bearer(opener, "token", "me@example.com", "12345678-1234-5678-1234-567812345678"))
        self.assertEqual(opener.open.call_count, 1)

    def test_nested_tokens_are_identified_without_values(self):
        text = "\n".join(login_response_summary({"Authorization": "Bearer secret-header"},
            b'{"data":{"accessToken":"secret-access","refresh_token":"secret-refresh","user":{"id":"secret-id"}},"message":"secret-message"}'))
        self.assertIn("response.data.accessToken: non-empty string", text)
        self.assertIn("response.data.refresh_token: non-empty string", text)
        self.assertIn("Response header present: Authorization", text)
        self.assertNotIn("secret-", text)

    def test_unknown_keys_and_non_json_do_not_leak(self):
        for body in [b'{"secret-key":"secret-value"}', b'<html>secret-token</html>', b'"secret-token"']:
            with self.subTest(body=body):
                text = "\n".join(login_response_summary({}, body))
                self.assertNotIn("secret-", text)

    def test_challenge_is_identified_without_exposing_response(self):
        lines = diagnostic_summary({"Content-Type": "text/html", "Server": "cloudflare",
                                    "Set-Cookie": "secret-token"},
                                   b"Just a moment; private-email@example.com secret-token")
        text = "\n".join(lines)
        self.assertIn("Browser challenge marker detected", text)
        self.assertIn("Gateway: Cloudflare detected", text)
        self.assertNotIn("secret-token", text)
        self.assertNotIn("private-email", text)

    def test_json_csrf_error_is_identified(self):
        lines = diagnostic_summary({"Content-Type": "application/json"}, b'{"message":"Invalid CSRF token"}')
        self.assertIn("Response format: JSON", lines)
        self.assertIn("CSRF / origin check marker detected", lines)

    def test_unknown_response_is_not_echoed(self):
        text = "\n".join(diagnostic_summary({"Server": "private", "Content-Type": "secret"}, b"token=secret"))
        self.assertNotIn("secret", text)
        self.assertNotIn("private", text)
