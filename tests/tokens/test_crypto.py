import pytest
from cryptography.fernet import Fernet

from postpilot.tokens.crypto import TokenCipher, TokenCryptoError


def test_round_trip_and_redacted_repr(monkeypatch):
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", key)
    cipher = TokenCipher.from_env()
    ciphertext = cipher.encrypt("example token 🧪")
    assert TokenCipher(key.encode()).decrypt(ciphertext) == "example token 🧪"
    assert key not in repr(cipher)


def test_wrong_key_and_malformed_ciphertext_are_redacted():
    key = Fernet.generate_key().decode()
    cipher = TokenCipher(key)
    plaintext = "example token"
    ciphertext = cipher.encrypt(plaintext)
    with pytest.raises(TokenCryptoError) as error:
        TokenCipher(Fernet.generate_key()).decrypt(ciphertext)
    assert all(value not in str(error.value) for value in (key, plaintext, ciphertext))
    for malformed in ("é", ciphertext + "!"):
        with pytest.raises(TokenCryptoError, match="Could not decrypt token"):
            cipher.decrypt(malformed)


@pytest.mark.parametrize("key", ["invalid key", "é", b""])
def test_malformed_key_is_rejected(key):
    with pytest.raises(TokenCryptoError, match="^Invalid token encryption key$"):
        TokenCipher(key)


def test_missing_key(monkeypatch):
    monkeypatch.delenv("TOKEN_ENCRYPTION_KEY", raising=False)
    with pytest.raises(TokenCryptoError, match="TOKEN_ENCRYPTION_KEY is missing"):
        TokenCipher.from_env()
