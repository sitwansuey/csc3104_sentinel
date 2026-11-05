"""Real requests through the caller and callee; no synthetic telemetry injection."""
import time
from urllib.error import URLError
from urllib.request import urlopen

while True:
    try:
        with urlopen("http://otel-caller:8080/work", timeout=5) as response:
            response.read()
    except URLError:
        pass
    time.sleep(2)
