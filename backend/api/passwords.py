from werkzeug.security import check_password_hash, generate_password_hash


def hash_password(plain_password: str) -> str:
    return generate_password_hash(plain_password)


def verify_password(plain_password: str, password_hash: str) -> bool:
    if not password_hash:
        return False
    return check_password_hash(password_hash, plain_password)
