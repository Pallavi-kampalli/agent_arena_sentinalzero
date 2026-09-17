import os
import sys
import time
from pyngrok import ngrok, conf
from dotenv import load_dotenv

load_dotenv()

token = os.getenv("NGROK_AUTHTOKEN", "3JS4EK1BZJRECcY6FqXv9GqRcOl_6Xgn5sUSa4NMXdkevcsFc")
domain = os.getenv("NGROK_URL", "plentiful-approve-prompter.ngrok-free.dev")
port = int(os.getenv("PORT", "8000"))

print(f"Setting ngrok authtoken...")
ngrok.set_auth_token(token)

print(f"Connecting ngrok tunnel to port {port} with domain '{domain}'...")
try:
    tunnel = ngrok.connect(port, "http", domain=domain)
    print(f"NGROK TUNNEL ESTABLISHED SUCCESSFULLY!")
    print(f"Public URL: {tunnel.public_url}")
    print(f"Forwarding to: http://127.0.0.1:{port}")
    
    # Keep process alive
    while True:
        time.sleep(1)
except Exception as e:
    print(f"Error establishing ngrok tunnel: {e}")
    sys.exit(1)
