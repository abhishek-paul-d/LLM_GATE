"""Model endpoint adapters. Only the OpenAI-compatible API is supported."""

from .openai_compat import Attempt, ChatClient, Completion, ErrorKind, chat_body

__all__ = ["Attempt", "ChatClient", "Completion", "ErrorKind", "chat_body"]
