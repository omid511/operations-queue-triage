"""Launch Streamlit and verify its health endpoint from the same process namespace."""

from __future__ import annotations

import subprocess
import sys
import time
from urllib.request import urlopen

PORT = "8501"
process = subprocess.Popen(
    [sys.executable, "-m", "streamlit", "run", "app.py", "--server.headless", "true", "--server.port", PORT, "--server.fileWatcherType", "none"],
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
    text=True,
)
try:
    for _ in range(30):
        try:
            with urlopen(f"http://127.0.0.1:{PORT}/_stcore/health", timeout=1) as response:
                if response.status == 200:
                    print("streamlit health ok")
                    break
        except OSError:
            time.sleep(1)
    else:
        output = process.stdout.read() if process.stdout else ""
        raise SystemExit(f"Streamlit health check failed.\n{output}")
finally:
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
