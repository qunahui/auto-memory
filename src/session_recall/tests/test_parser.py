"""Parser introspection tests — every subcommand has a TIER_MAP entry."""
from session_recall.__main__ import TIER_MAP


def test_tier_map_covers_all_subcommands():
    """Every registered subcommand must have a TIER_MAP entry."""
    known_commands = {"list", "schema-check", "files", "checkpoints", "show", "search", "health"}
    missing = known_commands - set(TIER_MAP.keys())
    assert not missing, f"Subcommands missing from TIER_MAP: {missing}"


def test_help_text_says_9_dimensions():
    """Regression guard against stale docstring."""
    from session_recall.commands import health
    assert "9 dimension" in health.__doc__.lower() or "9-dimension" in health.__doc__.lower()
