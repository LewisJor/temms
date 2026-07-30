"""Shared identifier slugification (#16/review DRY consolidation).

``package_builder`` and ``mission_compiler`` each grew a private ``_safe_id`` with
the *same name and intent* but *divergent behavior* — one kept ``.`` and preserved
case, the other stripped and lowercased. A single parameterized helper makes the
differences explicit instead of accidental, so no two call sites silently disagree
about what a "safe id" is.
"""

from __future__ import annotations


def slugify(
    value: str,
    *,
    extra_allowed: str = "-_",
    lowercase: bool = False,
    strip: str = "",
) -> str:
    """Replace unsafe characters with ``-``.

    Alphanumerics and any character in ``extra_allowed`` are kept; everything else
    becomes ``-``. Optionally ``strip`` leading/trailing characters and/or
    lowercase the result. Defaults are intentionally conservative; each caller
    states the policy it needs.
    """
    slug = "".join(ch if ch.isalnum() or ch in extra_allowed else "-" for ch in value)
    if strip:
        slug = slug.strip(strip)
    return slug.lower() if lowercase else slug
