"""How the studio talks to the model (llm.py is the only caller): requests and replies in
one neutral format (base.py) and their rendering for an OpenAI-compatible Chat Completions
API (openai_compat.py). Which model and key a request uses is the project's setting (llm.Model).
"""
from .base import ProviderError, Reply, Request  # noqa: F401  (re-exported)
