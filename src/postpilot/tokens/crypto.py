import base64
import binascii
import os

from cryptography.fernet import Fernet, InvalidToken


class TokenCryptoError(ValueError):
    pass


def _validate_encoding(value: bytes) -> None:
    decoded = base64.b64decode(value, altchars=b"-_", validate=True)
    if base64.urlsafe_b64encode(decoded) != value:
        raise ValueError("Expected canonical URL-safe Base64")


class TokenCipher:
    def __init__(self, key: str | bytes) -> None:
        try:
            encoded = key.encode("ascii") if isinstance(key, str) else key
            _validate_encoding(encoded)
            self._fernet = Fernet(encoded)
        except (ValueError, binascii.Error):
            pass
        else:
            return
        raise TokenCryptoError("Invalid token encryption key")

    @classmethod
    def from_env(cls) -> "TokenCipher":
        key = os.environ.get("TOKEN_ENCRYPTION_KEY")
        if not key:
            raise TokenCryptoError("TOKEN_ENCRYPTION_KEY is missing")
        return cls(key)

    def encrypt(self, plaintext: str) -> str:
        try:
            return self._fernet.encrypt(plaintext.encode()).decode("ascii")
        except UnicodeError:
            pass
        raise TokenCryptoError("Could not encode token for encryption")

    def decrypt(self, ciphertext: str) -> str:
        try:
            encoded = ciphertext.encode("ascii")
            _validate_encoding(encoded)
            return self._fernet.decrypt(encoded).decode()
        except (InvalidToken, ValueError, binascii.Error):
            pass
        raise TokenCryptoError("Could not decrypt token")
