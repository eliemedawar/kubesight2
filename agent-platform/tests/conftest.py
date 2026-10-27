import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")
os.environ.setdefault("AGENT_PLATFORM_TOKEN", "test-platform-token")
os.environ.setdefault("KUBESIGHT_AGENT_TOKEN", "ksa_test_agent_token_000000")
os.environ.setdefault("KUBESIGHT_URL", "http://kubesight.test")
os.environ.setdefault("MODEL_API_KEY", "test-key")
os.environ.setdefault("MODEL_BASE_URL", "https://ai-proxy.test/v1")
