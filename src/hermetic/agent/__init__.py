"""Agent stage: the Claude-backed implementations of the generator's interfaces."""

from __future__ import annotations

from hermetic.agent.claude import PROMPT_VERSION, ClaudeAuthor
from hermetic.agent.infer import INFER_PROMPT_VERSION, ClaudeInferrer

__all__ = ["INFER_PROMPT_VERSION", "PROMPT_VERSION", "ClaudeAuthor", "ClaudeInferrer"]
