import os
import json
import urllib.request
from dotenv import load_dotenv

load_dotenv()

resend_key = os.environ.get("RESEND_API_KEY")
resend_from = os.environ.get("RESEND_FROM_EMAIL")

print(f"Key: {resend_key}")
print(f"From: {resend_from}")

email_data = json.dumps({
    "from": resend_from,
    "to": ["ybanuka2003@gmail.com"],
    "subject": "Test from Resend",
    "html": "<p>Test email</p>"
}).encode('utf-8')

try:
    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=email_data,
        headers={
            "Authorization": f"Bearer {resend_key}",
            "Content-Type": "application/json",
            "User-Agent": "Resend-Python-Client/1.0"
        },
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        print("Success:", resp.read().decode())
except Exception as e:
    if hasattr(e, 'read'):
        print("Error from Resend API:", e.read().decode())
    else:
        print("Error:", str(e))
