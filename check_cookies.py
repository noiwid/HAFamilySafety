from cryptography.fernet import Fernet
import json, os, time

tmp = os.environ.get("TEMP", "C:\\Users\\benoi\\AppData\\Local\\Temp")
key = open(tmp + "\\cookies.key", "rb").read()
enc = open(tmp + "\\cookies.enc", "rb").read()
data = json.loads(Fernet(key).decrypt(enc).decode())
print("Timestamp:", data.get("timestamp"))
cookies = data.get("cookies", [])
print("Count:", len(cookies))
now = time.time()
for c in cookies:
    n = c.get("name", "")
    if n in ("canary", "MSPAuth", "MSPProf", "WLSSC", "RPSAuth"):
        exp = c.get("expires", -1)
        remaining = "session" if exp == -1 else f"{(exp - now)/3600:.1f}h left"
        print(f"  {n:20s} domain={c.get('domain',''):30s} {remaining}")
