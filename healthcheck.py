"""Docker healthcheck: the server is up if it answers (a 401 or redirect to /login still means up)."""
import sys
import urllib.error
import urllib.request

try:
    urllib.request.urlopen("http://127.0.0.1:8090/login", timeout=4)
except urllib.error.HTTPError as e:
    sys.exit(0 if e.code < 500 else 1)
except Exception:
    sys.exit(1)
