"""Generate a VAPID key pair and print the lines to put in your .env file."""
import base64
from cryptography.hazmat.primitives import serialization
from py_vapid import Vapid

b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()

v = Vapid()
v.generate_keys()
raw_private = v.private_key.private_numbers().private_value.to_bytes(32, "big")
public = v.public_key.public_bytes(
    serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
)
print(f"VAPID_PRIVATE_KEY={b64(raw_private)}")
print(f"VAPID_PUBLIC_KEY={b64(public)}")
print("VAPID_SUBJECT=mailto:you@example.com")
