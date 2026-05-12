"""
Compaction subpackage: re-exports utilities and settings for session compaction.

Owns: the import path for compaction shared types.
Delegates to: nuu.coding_agent.core.compaction.utils for implementation.

Depends on: nuu.coding_agent.core.compaction.utils
"""

from .utils import (
    FileOperations,
    CompactionSettings,
    DEFAULT_COMPACTION_SETTINGS,
    SUMMARIZATION_SYSTEM_PROMPT,
    create_file_ops,
    extract_file_ops_from_message,
    compute_file_lists,
    format_file_operations,
    truncate_for_summary,
    estimate_tokens,
    calculate_context_tokens,
    should_compact,
    find_cut_point,
    serialize_conversation,
)
from ...compaction import generate_summary, CompactionResult

__all__ = [
    "FileOperations",
    "CompactionSettings",
    "DEFAULT_COMPACTION_SETTINGS",
    "SUMMARIZATION_SYSTEM_PROMPT",
    "create_file_ops",
    "extract_file_ops_from_message",
    "compute_file_lists",
    "format_file_operations",
    "truncate_for_summary",
    "estimate_tokens",
    "calculate_context_tokens",
    "should_compact",
    "find_cut_point",
    "serialize_conversation",
    "generate_summary",
    "CompactionResult",
]
