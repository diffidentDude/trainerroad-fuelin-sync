"""Manage the locally saved Fuelin session."""
import argparse

from auth_store import KeychainStore, StorageError
from create_workout import FuelinClient, SyncError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["login", "logout", "status"])
    args = parser.parse_args()
    try:
        if args.action == "login":
            FuelinClient().login(refresh=True)
        elif args.action == "logout":
            KeychainStore().delete()
            print("Local Fuelin session removed from Keychain. Server-side session was not revoked.")
        else:
            print("Fuelin session saved in Keychain." if KeychainStore().load() else "No saved Fuelin session.")
        return 0
    except (StorageError, SyncError) as error:
        print(str(error))
    except Exception:
        print("Authentication request failed. Credentials and tokens were not printed.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
