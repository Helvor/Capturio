import base64
import hashlib
import hmac


def share_token(secret_key: str, password_hash: str) -> str:
    digest = hmac.new(secret_key.encode(), password_hash.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
