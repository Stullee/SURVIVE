"""The transport ignores proxy variables and ANTHROPIC_* variables."""
import os
os.environ["HTTPS_PROXY"] = "http://127.0.0.1:9"
os.environ["ALL_PROXY"] = "http://127.0.0.1:9"
os.environ["ANTHROPIC_BASE_URL"] = "https://evil.example"
from app.economy.anthropic_transport import AnthropicTransport
t = AnthropicTransport("sk-test")
c = t._client
print("base_url:", c.base_url, "| ANTHROPIC_BASE_URL left:", "ANTHROPIC_BASE_URL" in os.environ)
http = c._client
print("trust_env:", http.trust_env, "| mounts:", dict(http._mounts), "| transport:", type(http._transport).__name__)
