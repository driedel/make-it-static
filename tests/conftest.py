import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "api"))
sys.path.insert(0, str(Path(__file__).parent.parent / "worker"))

os.environ.setdefault("HMAC_SECRET", "testsecret")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379")
os.environ.setdefault("S3_BUCKET", "test-bucket")
