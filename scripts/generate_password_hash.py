#!/usr/bin/env python3
"""
Usage: python3 scripts/generate_password_hash.py <your_password>
Copy the output into ADMIN_PASSWORD_HASH in your .env file.

Requires: pip3 install bcrypt
Or via Docker (no local deps needed):
  docker run --rm python:3.12-slim sh -c \
    "pip install bcrypt -q && python3 -c \
    \"import bcrypt, sys; print(bcrypt.hashpw(sys.argv[1].encode(), bcrypt.gensalt()).decode())\" YOUR_PASSWORD"
"""
import sys
import subprocess

try:
    import bcrypt
except ModuleNotFoundError:
    print("Installing bcrypt...", file=sys.stderr)
    subprocess.check_call([sys.executable, "-m", "pip", "install", "bcrypt", "-q"])
    import bcrypt

if len(sys.argv) != 2:
    print("Usage: python3 scripts/generate_password_hash.py <password>")
    sys.exit(1)

hashed = bcrypt.hashpw(sys.argv[1].encode("utf-8"), bcrypt.gensalt())
print(hashed.decode("utf-8"))
