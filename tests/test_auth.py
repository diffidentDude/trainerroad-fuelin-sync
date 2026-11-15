import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

from create_workout import FuelinClient, SyncError


class StoredSessionTests(unittest.TestCase):
    def setUp(self):
        self.uid = "12345678-1234-5678-1234-567812345678"
        self.saved = {"email": "me@example.com", "token": "old-token", "user_id": self.uid}
        self.user = {"email": "me@example.com", "id": self.uid}
        self.store = MagicMock()
        self.store.load.return_value = self.saved
        self.client = FuelinClient()

    def test_cached_session_needs_no_credentials(self):
        self.client.request = MagicMock(return_value=self.user)
        with patch("builtins.input") as email, patch("getpass.getpass") as password, redirect_stdout(io.StringIO()):
            self.assertEqual(self.client.login(store=self.store), self.user)
        email.assert_not_called()
        password.assert_not_called()
        self.assertEqual(self.client.token, "old-token")
        self.assertEqual(self.client.request.call_count, 1)

    def test_expired_session_logs_in_and_saves_no_password(self):
        self.client.request = MagicMock(side_effect=[HTTPError("https://prod.fuelin.io", 401, "expired", {}, io.BytesIO()), {"token": "new-token"}, self.user])
        with patch("getpass.getpass", return_value="secret-password"), patch("create_workout.session_user_id", return_value=self.uid), redirect_stdout(io.StringIO()):
            self.client.login(store=self.store)
        self.store.delete.assert_called_once()
        self.store.save.assert_called_once_with({**self.saved, "token": "new-token"})

    def test_forbidden_does_not_erase_session_or_prompt(self):
        error = HTTPError("https://prod.fuelin.io", 403, "forbidden", {}, io.BytesIO())
        self.addCleanup(error.close)
        self.client.request = MagicMock(side_effect=error)
        with patch("getpass.getpass") as password, self.assertRaises(HTTPError):
            self.client.login(store=self.store)
        self.store.delete.assert_not_called()
        password.assert_not_called()

    def test_identity_mismatch_is_not_saved(self):
        self.store.load.return_value = None
        self.client.request = MagicMock(side_effect=[{"token": "new-token"}, {**self.user, "email": "other@example.com"}])
        with patch("builtins.input", return_value="me@example.com"), patch("getpass.getpass", return_value="password"), patch("create_workout.session_user_id", return_value=self.uid), self.assertRaises(SyncError):
            self.client.login(store=self.store)
        self.store.save.assert_not_called()

    def test_refresh_replaces_token_without_reading_old_profile(self):
        self.client.request = MagicMock(side_effect=[{"token": "new-token"}, self.user])
        with patch("getpass.getpass", return_value="password"), patch("create_workout.session_user_id", return_value=self.uid), redirect_stdout(io.StringIO()):
            self.client.login(refresh=True, store=self.store)
        self.assertEqual(self.client.request.call_args_list[0].args[0], "POST")
        self.store.save.assert_called_once_with({**self.saved, "token": "new-token"})
