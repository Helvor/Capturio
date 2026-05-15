#!/usr/bin/env python3
"""
Usage: python3 scripts/generate_password_hash.py <your_password>
Copy the output into ADMIN_PASSWORD_HASH in your .env file.

The $ signs are escaped as $$ so Docker Compose doesn't interpret them as
variables. The app receives the correct hash at runtime.

Requires: pip3 install bcrypt
Or via Docker (no local deps needed):
  docker run --rm python:3.12-slim sh -c \
    "pip install bcrypt -q && python3 -c \
    \"import bcrypt,sys; h=bcrypt.hashpw(sys.argv[1].encode(),bcrypt.gensalt()).decode(); print(h.replace('\$','$$'))\" YOUR_PASSWORD"
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

hashed = bcrypt.hashpw(sys.argv[1].encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
# Escape $ as $$ so Docker Compose doesn't treat them as variable references
print(hashed.replace("$", "$$"))
