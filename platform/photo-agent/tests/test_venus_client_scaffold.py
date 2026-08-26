"""
Tests for the venus client scaffold (issue #12) under the single-install
architecture (issue #61).

Venus's whole premise is that swapping the model provider (Anthropic -> OpenAI)
requires no change to the photo-agent pipeline itself -- only to which client
is installed as the active one via
platform/photo-agent/scripts/install_client.sh. These tests guard that claim
structurally: venus's .env.example must declare exactly the same
configuration keys as _demo's (pipeline config plus the shared "Hermes
gateway install" fields), so a diff between the two clients' env files shows
nothing but comments.
"""

from pathlib import Path

_ROOT = Path(__file__).parents[3]
_DEMO_ENV_EXAMPLE = _ROOT / "clients" / "_demo" / "src" / "photo-agent" / ".env.example"
_VENUS_ENV_EXAMPLE = _ROOT / "clients" / "venus" / "src" / "photo-agent" / ".env.example"
_VENUS_README = _ROOT / "clients" / "venus" / "README.md"
_VENUS_CONSTITUTION = _ROOT / "clients" / "venus" / ".specify" / "constitution.md"
_PROFILES_DOC = _ROOT / "platform" / "docs" / "hermes" / "09-per-client-model-profiles.md"


def _env_keys(path: Path) -> set[str]:
    """Extract KEY names from a dotenv-style file, ignoring comments/blanks."""
    keys = set()
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        keys.add(stripped.split("=", 1)[0].strip())
    return keys


def test_venus_env_example_exists():
    assert _VENUS_ENV_EXAMPLE.exists()


def test_venus_env_example_matches_demo_key_set():
    """Venus and _demo must configure the identical set of variables --
    both the pipeline config and the shared "Hermes gateway install" fields
    (TELEGRAM_ALLOWED_USERS, HERMES_MODEL_PROVIDER, HERMES_MODEL_DEFAULT,
    HERMES_PROVIDER_API_KEY) that install_client.sh reads. Provider choice
    (Anthropic vs OpenAI) is a VALUE difference in those shared keys, never
    a difference in which keys exist."""
    demo_keys = _env_keys(_DEMO_ENV_EXAMPLE)
    venus_keys = _env_keys(_VENUS_ENV_EXAMPLE)
    assert venus_keys == demo_keys


def test_venus_env_example_declares_hermes_install_fields():
    keys = _env_keys(_VENUS_ENV_EXAMPLE)
    for required in (
        "TELEGRAM_ALLOWED_USERS",
        "HERMES_MODEL_PROVIDER",
        "HERMES_MODEL_DEFAULT",
        "HERMES_PROVIDER_API_KEY",
    ):
        assert required in keys, f"venus .env.example is missing {required}"


def test_venus_readme_documents_provider_configuration():
    text = _VENUS_README.read_text()
    assert "openai-api" in text
    assert "Provider Configuration" in text


def test_venus_readme_documents_install_client_script():
    """Regression guard for issue #61: venus's setup checklist must point
    at install_client.sh, not at the retired per-client Hermes profile
    mechanism (hermes profile create / hermes -p venus ...)."""
    text = _VENUS_README.read_text()
    assert "install_client.sh venus" in text
    assert "hermes profile create venus" not in text
    assert "hermes -p venus" not in text


def test_venus_readme_documents_required_install_fields():
    text = _VENUS_README.read_text()
    assert "TELEGRAM_ALLOWED_USERS" in text
    assert "HERMES_PROVIDER_API_KEY" in text
    assert "skills.external_dirs" in text


def test_venus_readme_ad_hoc_test_uses_inline_override_not_the_installer():
    """The "run the e2e rig against venus without installing it" path must
    keep using the ad-hoc CLIENT_NAME= inline override (issue #45/PR #57,
    kept as an escape hatch by #61) -- not install_client.sh, which would
    make venus the live installed client and disrupt whatever's actually
    running."""
    text = _VENUS_README.read_text()
    assert "CLIENT_NAME=venus FIELDKIT_ROOT=" in text


def test_venus_constitution_exists():
    assert _VENUS_CONSTITUTION.exists()


def test_venus_readme_points_to_shared_allowlist_troubleshooting_doc():
    """Regression guard for a wrong verification method caught in review
    (pre-#61): gateway/run.py's "No env user allowlists configured"
    warning is a disjunction over ~20 platform-specific allowlist env
    vars -- its absence proves nothing about TELEGRAM_ALLOWED_USERS
    specifically. That troubleshooting content now lives in the shared
    09-per-client-model-profiles.md doc (issue #61 -- it applies identically
    regardless of which client is installed), not duplicated per-client;
    venus's README must at least point there."""
    text = _VENUS_README.read_text()
    assert "09-per-client-model-profiles.md" in text
    assert "troubleshooting" in text.lower()


def _find_bullet(text: str, needle: str) -> str:
    """Return the single markdown bullet (a "- ..." line plus any wrapped
    continuation lines up to the next bullet or blank line) whose first line
    contains `needle` (case-insensitive), joined into one string.

    Fails loudly (not just returns "") if zero or more than one bullet's
    first line matches, so this helper can't silently pass a scoped
    assertion against text that no longer has the expected shape. Scans
    multi-line bullets (not just a single line) so it stays correct across
    markdown re-wrapping that moves a key phrase onto a continuation line.
    """
    lines = text.splitlines()
    starts = [
        i for i, line in enumerate(lines)
        if line.lstrip().startswith("- ") and needle.lower() in line.lower()
    ]
    assert len(starts) == 1, (
        f"expected exactly one bullet starting with a line containing {needle!r}, "
        f"found {len(starts)}"
    )
    start = starts[0]
    end = start + 1
    while end < len(lines) and lines[end].strip() and not lines[end].lstrip().startswith("- "):
        end += 1
    return " ".join(lines[start:end])


def test_shared_doc_verifies_telegram_allowlist_correctly():
    text = _PROFILES_DOC.read_text()
    assert "grep '^TELEGRAM_ALLOWED_USERS=' ~/.hermes/.env" in text
    assert "fail-closed" in text.lower()
    # The wrong method (checking the global startup warning) may still be
    # mentioned as a documented gotcha, but never as the thing to *do*.
    assert "check `~/.hermes/profiles/venus/logs/gateway.log` for the" not in text


def test_shared_doc_distinguishes_the_two_fail_closed_symptoms():
    """Regression guard for a conflated failure mode caught in review.

    "Fail-closed" is not one symptom -- gateway/authz_mixin.py's
    _get_unauthorized_dm_behavior resolves to "pair" when no allowlist is
    configured at all (TELEGRAM_ALLOWED_USERS unset -> admin gets an
    unexpected pairing-code prompt), but to "ignore" when an allowlist IS
    configured and simply doesn't match (TELEGRAM_ALLOWED_USERS set wrong
    -> admin is silently dropped, no prompt, no response).

    Scoped to each cause's own line (not "does the file contain both
    phrases somewhere") so that swapping which symptom is attributed to
    which cause -- the actual mistake this guards against -- fails the
    test, rather than passing because both phrases still appear in the
    file. This content now lives in the shared 09-per-client-model-profiles.md
    doc (issue #61), since it's the same regardless of which client is
    installed -- not duplicated in every client README.
    """
    text = _PROFILES_DOC.read_text()

    # "pairing-code prompt" (not the bare word "pairing") is the
    # discriminator: the mistyped-allowlist bullet legitimately says "no
    # pairing prompt" in passing, so a bare "pairing" substring check
    # would match both bullets and not actually guard against a swap.
    unset_bullet = _find_bullet(text, "left unset entirely")
    assert "pairing-code prompt" in unset_bullet.lower()
    assert "silently ignored" not in unset_bullet.lower()

    mistyped_bullet = _find_bullet(text, "wrong/mistyped")
    assert "silently ignored" in mistyped_bullet.lower()
    assert "pairing-code prompt" not in mistyped_bullet.lower()
