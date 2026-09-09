"""Production adapter for the optimized paper-extraction harness."""

from .harness import (
    HARNESS_VERSION,
    OPTIMIZED_PAPER_INSTRUCTION,
    apply_semantic_extraction,
    build_parsed_context,
    extract_semantic_content,
    validate_semantic_extraction,
)

__all__ = [
    "HARNESS_VERSION",
    "OPTIMIZED_PAPER_INSTRUCTION",
    "apply_semantic_extraction",
    "build_parsed_context",
    "extract_semantic_content",
    "validate_semantic_extraction",
]
