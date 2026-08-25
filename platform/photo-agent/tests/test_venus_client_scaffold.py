"""
Tests for the venus client scaffold (issue #12).

Venus's whole premise is that swapping the model provider (Anthropic -> OpenAI)
requires no change to the photo-agent pipeline itself -- only to which Hermes
profile executes it. These tests guard that claim structurally: venus's
.env.example must declare exactly the same pipeline configuration keys as
_demo's, so a diff between the two clients' env files shows nothing but
comments.
"""

from pathlib import Path

_ROOT = Path(__file__).parents[3]
_DEMO_ENV_EXAMPLE = _ROOT / "clients" / "_demo" / "src" / "photo-agent" / ".env.example"
_VENUS_ENV_EXAMPLE = _ROOT / "clients" / "venus" / "src" / "photo-agent" / ".env.example"
_VENUS_README = _ROOT / "clients" / "venus" / "README.md"
_VENUS_CONSTITUTION = _ROOT / "clients" / "venus" / ".specify" / "constitution.md"


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
    """Venus and _demo must configure the identical set of pipeline variables.

    Provider choice (Anthropic vs OpenAI) lives entirely in the client's
    Hermes profile, not in this file -- so the two clients' .env.example
    key sets must be identical.
    """
    demo_keys = _env_keys(_DEMO_ENV_EXAMPLE)
    venus_keys = _env_keys(_VENUS_ENV_EXAMPLE)
    assert venus_keys == demo_keys


def test_venus_readme_documents_provider_configuration():
    text = _VENUS_README.read_text()
    assert "openai-api" in text
    assert "Provider Configuration" in text


def test_venus_readme_scopes_telegram_token_and_skills_to_the_profile():
    """Regression guard for the profile-isolation gaps caught in review:

    a fresh Hermes profile inherits neither the default profile's Telegram
    bot token nor its skills.external_dirs -- both must be set on the
    venus profile specifically, not assumed from clients/venus's own .env
    or from the default profile's ~/.hermes/config.yaml.
    """
    text = _VENUS_README.read_text()
    assert "profiles/venus/.env" in text
    assert "skills.external_dirs" in text


def test_venus_readme_does_not_tell_a_human_to_edit_the_shared_root_env():
    """CLIENT_NAME lives in one shared fieldkit/.env read by every client's
    cron jobs (including _demo's, live on this machine) -- the e2e
    instructions must use an inline override, never a persistent edit.
    """
    text = _VENUS_README.read_text()
    assert "# fieldkit/.env\nCLIENT_NAME=venus" not in text
    assert "CLIENT_NAME=venus FIELDKIT_ROOT=" in text


def test_venus_constitution_exists():
    assert _VENUS_CONSTITUTION.exists()
