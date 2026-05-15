#!/usr/bin/env python3
"""
Usage: python scripts/generate_password_hash.py <your_password>
Copy the output into ADMIN_PASSWORD_HASH in your .env file.
"""
import sys
from passlib.context import CryptContext

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

if len(sys.argv) != 2:
    print("Usage: python scripts/generate_password_hash.py <password>")
    sys.exit(1)

password = sys.argv[1]
hashed = pwd_context.hash(password)
print(hashed)
