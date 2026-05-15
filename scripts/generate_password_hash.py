#!/usr/bin/env python3
"""
Usage: python3 scripts/generate_password_hash.py <your_password>
Copy the output into ADMIN_PASSWORD_HASH in your .env file.

Requires: pip3 install passlib[bcrypt]
Or run via Docker (no local deps needed):
  docker run --rm python:3.12-slim sh -c \
    "pip install passlib[bcrypt] -q && python3 -c \
    \"from passlib.context import CryptContext; print(CryptContext(schemes=['bcrypt']).hash('YOUR_PASSWORD'))\""
"""
import sys
import subprocess

try:
    from passlib.context import CryptContext
except ModuleNotFoundError:
    print("Installing passlib[bcrypt]...", file=sys.stderr)
    subprocess.check_call([sys.executable, "-m", "pip", "install", "passlib[bcrypt]", "-q"])
    from passlib.context import CryptContext

if len(sys.argv) != 2:
    print("Usage: python3 scripts/generate_password_hash.py <password>")
    sys.exit(1)

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
print(pwd_context.hash(sys.argv[1]))
