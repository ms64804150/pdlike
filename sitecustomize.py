"""Loaded only when running from source. Frozen builds must not ship this filename."""

try:
    from perfpilot.win_compat import apply

    apply()
except Exception:
    pass
