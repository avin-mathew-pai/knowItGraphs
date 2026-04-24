"""Pluggable LLM adapter via litellm.

Switch providers by setting LLM_PROVIDER in .env — no code change needed.
"""
from __future__ import annotations

import logging
import os
from typing import AsyncIterator, Dict, List

import litellm

from .config import SETTINGS

log = logging.getLogger(__name__)

# Suppress noisy litellm banner
litellm.suppress_debug_info = True


class LLMError(RuntimeError):
    """Raised when the LLM call fails with a user-recoverable reason (bad key, quota)."""


def _resolve_model_and_key() -> tuple[str, Dict[str, str]]:
    provider = SETTINGS.provider
    if provider == "gemini":
        if not SETTINGS.gemini_api_key:
            raise LLMError("GEMINI_API_KEY is empty. Get a free key at https://aistudio.google.com/apikey and put it in .env.")
        os.environ["GEMINI_API_KEY"] = SETTINGS.gemini_api_key
        return SETTINGS.gemini_model, {}
    if provider == "anthropic":
        if not SETTINGS.anthropic_api_key:
            raise LLMError("ANTHROPIC_API_KEY is empty. Get one at https://console.anthropic.com and put it in .env.")
        os.environ["ANTHROPIC_API_KEY"] = SETTINGS.anthropic_api_key
        return SETTINGS.anthropic_model, {}
    if provider == "openai":
        if not SETTINGS.openai_api_key:
            raise LLMError("OPENAI_API_KEY is empty.")
        os.environ["OPENAI_API_KEY"] = SETTINGS.openai_api_key
        return SETTINGS.openai_model, {}
    if provider == "ollama":
        return SETTINGS.ollama_model, {"api_base": SETTINGS.ollama_base_url}
    raise LLMError(f"Unknown LLM_PROVIDER '{provider}'. Use: gemini | anthropic | openai | ollama.")


async def chat(messages: List[Dict[str, str]], temperature: float = 0.2, max_tokens: int = 1500) -> str:
    """Call the configured LLM with a standard OpenAI-style messages array.

    Raises LLMError with an actionable message on known failures.
    """
    model, extra = _resolve_model_and_key()
    try:
        resp = await litellm.acompletion(
            model=model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            **extra,
        )
    except litellm.AuthenticationError as e:
        raise LLMError(f"Auth failed for provider '{SETTINGS.provider}': {e}") from e
    except litellm.RateLimitError as e:
        raise LLMError(f"Rate-limited by '{SETTINGS.provider}'. Try again shortly or switch providers in .env. ({e})") from e
    except litellm.APIConnectionError as e:
        raise LLMError(f"Could not reach '{SETTINGS.provider}' endpoint. Network/URL issue. ({e})") from e
    except Exception as e:
        log.exception("LLM call failed.")
        raise LLMError(f"LLM call failed: {e}") from e

    try:
        return resp.choices[0].message.content or ""
    except (AttributeError, IndexError) as e:
        raise LLMError(f"Unexpected response shape from provider: {e}") from e


async def chat_stream(
    messages: List[Dict[str, str]],
    temperature: float = 0.2,
    max_tokens: int = 1500,
) -> AsyncIterator[str]:
    """Stream tokens from the configured LLM. Yields text fragments."""
    model, extra = _resolve_model_and_key()
    try:
        stream = await litellm.acompletion(
            model=model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            stream=True,
            **extra,
        )
    except litellm.AuthenticationError as e:
        raise LLMError(f"Auth failed for provider '{SETTINGS.provider}': {e}") from e
    except litellm.RateLimitError as e:
        raise LLMError(f"Rate-limited by '{SETTINGS.provider}'. Try again shortly or switch providers in .env. ({e})") from e
    except litellm.APIConnectionError as e:
        raise LLMError(f"Could not reach '{SETTINGS.provider}' endpoint. Network/URL issue. ({e})") from e
    except Exception as e:
        log.exception("LLM stream open failed.")
        raise LLMError(f"LLM call failed: {e}") from e

    try:
        async for chunk in stream:
            try:
                delta = chunk.choices[0].delta.content
            except (AttributeError, IndexError):
                delta = None
            if delta:
                yield delta
    except Exception as e:
        log.exception("LLM stream interrupted.")
        raise LLMError(f"LLM stream interrupted: {e}") from e


def describe_provider() -> Dict[str, str]:
    return {
        "provider": SETTINGS.provider,
        "model": {
            "gemini": SETTINGS.gemini_model,
            "anthropic": SETTINGS.anthropic_model,
            "openai": SETTINGS.openai_model,
            "ollama": SETTINGS.ollama_model,
        }.get(SETTINGS.provider, "unknown"),
    }
