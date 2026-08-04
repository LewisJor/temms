"""Shared slugify helper (#16/review DRY consolidation)."""

from temms.core.identifiers import slugify


def test_package_builder_policy_keeps_dot_and_case():
    # package/model ids: keep '.', preserve case, no strip.
    assert slugify("Vision-1.2.0", extra_allowed="-_.") == "Vision-1.2.0"
    assert slugify("a b/c", extra_allowed="-_.") == "a-b-c"


def test_compiler_policy_lowercases_and_strips():
    # on-disk dir names: drop '.', lowercase, strip leading/trailing '-'.
    assert slugify("Vision-1.2.0", extra_allowed="-_", strip="-", lowercase=True) == "vision-1-2-0"
    assert slugify("--Weird..Name--", extra_allowed="-_", strip="-", lowercase=True) == "weird--name"


def test_default_is_conservative():
    assert slugify("a.b c") == "a-b-c"  # '.' not allowed by default
