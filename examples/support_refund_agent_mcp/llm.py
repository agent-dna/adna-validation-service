"""The LLM, picked in .env with LLM_BACKEND: openai (any OpenAI-compatible service) or gemini."""

import os


def build_llm():
    backend = os.getenv("LLM_BACKEND", "openai").lower()
    temperature = float(os.getenv("LLM_TEMPERATURE", "0"))
    if backend == "openai":
        # OpenAI, or any OpenAI-compatible service at OPENAI_BASE_URL:
        # Ollama is http://127.0.0.1:11434/v1 (with any OPENAI_API_KEY, e.g. "ollama")
        from langchain_openai import ChatOpenAI  # reads OPENAI_API_KEY

        return ChatOpenAI(
            model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
            base_url=os.getenv("OPENAI_BASE_URL") or None,
            temperature=temperature,
        )
    if backend == "gemini":
        from langchain_google_genai import ChatGoogleGenerativeAI  # reads GOOGLE_API_KEY

        return ChatGoogleGenerativeAI(
            model=os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite"), temperature=temperature
        )
    raise ValueError(f"Unsupported LLM_BACKEND: {backend} (openai or gemini)")
