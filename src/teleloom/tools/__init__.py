"""Shared typed MCP parameter/result aliases, preserving the public wire schemas."""

from typing import Annotated

from mcp.types import CallToolResult
from pydantic import Field

from ..models import EvidenceKey, Result

ToolResult = Annotated[CallToolResult, Result]
PageLimit = Annotated[int, Field(ge=1, le=100)]
OutputBudget = Annotated[
    int,
    Field(
        ge=1,
        le=2 * 1024 * 1024,
        description="Opt-in byte cap of normalized compact UTF-8 data, including identity, coverage, warnings and budget/reference metadata. Long selected content becomes a marked excerpt; full originals are frozen for exact retrieval. Wire and MCP representations are larger and measured separately.",
    ),
]
EvidenceKeys = Annotated[
    list[EvidenceKey],
    Field(
        min_length=1,
        max_length=100,
        description="Exact frozen (chat_id, message_id) keys to read from the referenced evidence snapshot; each must exist in that snapshot.",
    ),
]
ResponseFields = Annotated[
    list[str],
    Field(
        description="Optional flat record field names from response_fields_select. Source identities, date, links and coverage cannot be removed. Omit for the full response."
    ),
]
ResponsePreset = Annotated[
    str,
    Field(
        description="full, minimal, compact, digest, authors, engagement or attachments. Cannot be combined with fields."
    ),
]
