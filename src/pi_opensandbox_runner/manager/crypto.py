from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken

from .problems import ManagerProblem


class CredentialCipher:
    def __init__(self, key: str):
        self.key = key

    def _fernet(self) -> Fernet:
        if not self.key:
            raise ManagerProblem(
                503,
                "credential_key_missing",
                "RUNNER_MANAGER_CREDENTIAL_ENCRYPTION_KEY is not configured",
            )
        try:
            return Fernet(self.key.encode())
        except (ValueError, TypeError) as exc:
            raise ManagerProblem(
                503,
                "credential_key_invalid",
                "runner manager credential encryption key is invalid",
            ) from exc

    def encrypt(self, value: str) -> str:
        return self._fernet().encrypt(value.encode()).decode()

    def decrypt(self, value: str) -> str:
        try:
            return self._fernet().decrypt(value.encode()).decode()
        except InvalidToken as exc:
            raise ManagerProblem(
                503, "credential_decryption_failed", "stored credential is invalid"
            ) from exc
