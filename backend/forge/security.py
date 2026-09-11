import base64
import hashlib
import hmac
import os
from cryptography.fernet import Fernet
from urllib.parse import urlparse
from .config import settings

def password_hash(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1)
    return base64.b64encode(salt + digest).decode()

def password_matches(password: str, encoded: str) -> bool:
    try:
        raw = base64.b64decode(encoded)
        check = hashlib.scrypt(password.encode(), salt=raw[:16], n=16384, r=8, p=1)
        return hmac.compare_digest(check, raw[16:])
    except (ValueError, TypeError):
        return False

def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()

def cipher() -> Fernet:
    if not settings.encryption_key:
        raise ValueError('缺少 ENCRYPTION_KEY，请运行 scripts/init.sh')
    return Fernet(settings.encryption_key.encode())

def encrypt(value: str) -> str:
    return cipher().encrypt(value.encode()).decode() if value else ''

def decrypt(value: str) -> str:
    return cipher().decrypt(value.encode()).decode() if value else ''

def validate_url(url: str) -> str:
    p = urlparse(url)
    if p.scheme not in ('http', 'https') or not p.hostname or p.username or p.password or p.query or p.fragment:
        raise ValueError('接口地址必须是无凭据、查询串和片段的 HTTP(S) URL')
    # Credentials belong in the encrypted secret field, never in a URL.
    return url.rstrip('/')
