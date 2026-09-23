import os
import urllib.request
import urllib.error

url = "https://api.groq.com/openai/v1/models"

req = urllib.request.Request(
    url,
    headers={
        "Authorization": "Bearer " + os.environ["GROQ_API_KEY"]
    }
)

try:
    response = urllib.request.urlopen(req, timeout=30)
    print("STATUS:", response.status)
    print("BODY:")
    print(response.read().decode(errors="replace")[:3000])

except urllib.error.HTTPError as e:
    print("STATUS:", e.code)
    print("BODY:")
    print(e.read().decode(errors="replace")[:3000])

except Exception as e:
    print("ERROR:", type(e).__name__, str(e))
