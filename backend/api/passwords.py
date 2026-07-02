import secrets
import string

from werkzeug.security import check_password_hash, generate_password_hash

# Character classes used to build temporary passwords. Ambiguous characters
# (O/0, l/1/I) are excluded so the value survives being copied out of an email.
_TEMP_UPPER = "ABCDEFGHJKLMNPQRSTUVWXYZ"
_TEMP_LOWER = "abcdefghijkmnopqrstuvwxyz"
_TEMP_DIGITS = "23456789"
_TEMP_SYMBOLS = "!@#$%^&*?-_"


def hash_password(plain_password: str) -> str:
    return generate_password_hash(plain_password)


def verify_password(plain_password: str, password_hash: str) -> bool:
    if not password_hash:
        return False
    return check_password_hash(password_hash, plain_password)


def generate_temporary_password(length: int = 16) -> str:
    """Return a cryptographically-random temporary password.

    Guarantees at least one character from each class (upper, lower, digit,
    symbol) so the value satisfies common password-strength rules, then fills the
    remainder from the combined alphabet and shuffles with a CSPRNG.
    """
    length = max(12, int(length))
    alphabet = _TEMP_UPPER + _TEMP_LOWER + _TEMP_DIGITS + _TEMP_SYMBOLS
    required = [
        secrets.choice(_TEMP_UPPER),
        secrets.choice(_TEMP_LOWER),
        secrets.choice(_TEMP_DIGITS),
        secrets.choice(_TEMP_SYMBOLS),
    ]
    remaining = [secrets.choice(alphabet) for _ in range(length - len(required))]
    chars = required + remaining
    # Fisher-Yates shuffle driven by secrets so class positions aren't fixed.
    for i in range(len(chars) - 1, 0, -1):
        j = secrets.randbelow(i + 1)
        chars[i], chars[j] = chars[j], chars[i]
    return "".join(chars)
