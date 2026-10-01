"""Tests for credential exclusion + secret scrubbing during profile export.

Profile exports are documented as shareable with keys stripped. They must NEVER
include credential stores Hermes reads from a profile home — API keys, OAuth
tokens, and credential pool data. Users share exported profiles; leaking
credentials in the archive is a security issue.

Secret-shaped strings that sneak into skills / persona / memory text are
force-redacted in the staged archive (same pass as sessions --redact).
The live profile on disk must stay untouched.
"""

import tarfile
from pathlib import Path

import pytest

from hermes_cli.profiles import export_profile

# Long enough to match agent.redact prefix patterns (sk- + 10+ chars).
_LEAKED_KEY = "sk-or-v1-reallyLongSecretKeyValue12345678"


def _patch_named_profile(monkeypatch, profiles_root, profile_dir):
    monkeypatch.setattr("hermes_cli.profiles._get_profiles_root", lambda: profiles_root)
    monkeypatch.setattr("hermes_cli.profiles.get_profile_dir", lambda n: profile_dir)
    monkeypatch.setattr("hermes_cli.profiles.validate_profile_name", lambda n: None)


class TestCredentialExclusion:

    def test_named_profile_export_excludes_auth(self, tmp_path, monkeypatch):
        """Named profile export must not contain auth.json or .env."""
        profiles_root = tmp_path / "profiles"
        profile_dir = profiles_root / "testprofile"
        profile_dir.mkdir(parents=True)

        # Create a profile with credentials
        (profile_dir / "config.yaml").write_text("model: gpt-4\n")
        (profile_dir / "auth.json").write_text('{"tokens": {"access": "sk-secret"}}')
        (profile_dir / ".env").write_text("OPENROUTER_API_KEY=sk-secret-key\n")
        (profile_dir / "SOUL.md").write_text("I am helpful.\n")
        (profile_dir / "memories").mkdir()
        (profile_dir / "memories" / "MEMORY.md").write_text("# Memories\n")

        _patch_named_profile(monkeypatch, profiles_root, profile_dir)

        output = tmp_path / "export.tar.gz"
        result = export_profile("testprofile", str(output))

        # Check archive contents
        with tarfile.open(result, "r:gz") as tf:
            names = tf.getnames()

        assert any("config.yaml" in n for n in names), "config.yaml should be in export"
        assert any("SOUL.md" in n for n in names), "SOUL.md should be in export"
        assert not any("auth.json" in n for n in names), "auth.json must NOT be in export"
        assert not any(Path(n).name == ".env" for n in names), ".env must NOT be in export"


# ---------------------------------------------------------------------------
# Issue #126114: every credential store Hermes reads from a profile home must
# be stripped from named-profile exports, not just auth.json / .env /
# bot-desktop. Behaviour contract (not a snapshot): the archive keeps persona
# files and drops every credential basename / subtree, at any depth.
# ---------------------------------------------------------------------------

_CREDENTIAL_ROOT_FILES = [
    "auth.json", "auth.lock",
    ".env", ".op.env", ".anthropic_oauth.json",
    ".npmrc", "npmrc", ".pypirc",
    ".netrc", ".pgpass", ".git-credentials",
    "google_oauth.json", "google_token.json", "google_oauth_pending.json",
    "google_chat_user_token.json", "google_chat_user_client_secret.json",
    "google_chat_user_oauth_pending.json",
    "slack_tokens.json",
    "webhook_subscriptions.json", "feishu_comment_pairing.json",
    "vault.key", "vault.json.enc",
    "bws_cache.json", "bws_cache.enc.json",
    "channel_directory.json", "channel_aliases.json",
    ".env.local",
]

# Credential subtrees seeded under the profile root: (top, relative leaf).
# Every *top* except ``cache`` is pruned wholesale (the dir itself must vanish
# from the archive); ``cache/`` stays (it holds non-credential caches) while its
# Bitwarden files are stripped.
_CREDENTIAL_TREES = [
    ("bot-desktop", "browser-profile/Default/Cookies"),
    ("mcp-tokens", "server.json"),
    ("pairing", "telegram.json"),
    ("vault", "vault.key"),
    ("browser-profile", "chrome/Default/Cookies"),
    ("browser_auth", "camofox/state.json"),
    ("credentials", "github_token"),
    ("platforms", "pairing/x.json"),
    ("platforms", "whatsapp/session/creds.json"),
    ("platforms", "matrix/store/crypto.db"),
    ("whatsapp", "session/creds.json"),
    ("matrix", "store/crypto.db"),
    ("auth", "google_oauth.json"),
    ("cache", "bws_cache.json"),
    ("cache", "bws_cache.enc.json"),
    ("google_chat_user_tokens", "u@x.json"),
    ("google_chat_user_oauth_pending", "u@x.json"),
    ("gateway", "discord_message_recovery.db"),
]

_FULL_DIR_TOPS = frozenset(
    top for top, _rel in _CREDENTIAL_TREES if top != "cache"
)


def _seed_credential_profile(profile_dir: Path) -> None:
    """Populate *profile_dir* with persona files plus every credential store."""
    (profile_dir / "config.yaml").write_text("model: gpt-4\n", encoding="utf-8")
    (profile_dir / "SOUL.md").write_text("I am helpful.\n", encoding="utf-8")
    memories = profile_dir / "memories"
    memories.mkdir(parents=True, exist_ok=True)
    (memories / "MEMORY.md").write_text("# Memories\n", encoding="utf-8")
    skill_dir = profile_dir / "skills" / "demo"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: demo\ndescription: Demo.\n---\nHelpful.\n", encoding="utf-8"
    )
    # Shareable dotenv shape substitute — must survive the export.
    (profile_dir / ".env.example").write_text("OPENROUTER_API_KEY=\n", encoding="utf-8")

    for name in _CREDENTIAL_ROOT_FILES:
        (profile_dir / name).write_text("secret-material", encoding="utf-8")
    for top, rel in _CREDENTIAL_TREES:
        target = profile_dir / top / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"secret-material")


def _archive_basenames(archive: Path) -> set[str]:
    with tarfile.open(archive, "r:gz") as tf:
        return {Path(n).name for n in tf.getnames()}


def _archive_names(archive: Path) -> list[str]:
    with tarfile.open(archive, "r:gz") as tf:
        return tf.getnames()


class TestComprehensiveCredentialExclusion:
    def test_named_export_drops_every_credential_store(self, tmp_path, monkeypatch):
        """All credential files / secret dirs are stripped; persona survives."""
        profiles_root = tmp_path / "profiles"
        profile_dir = profiles_root / "leaky"
        profile_dir.mkdir(parents=True)
        _seed_credential_profile(profile_dir)
        _patch_named_profile(monkeypatch, profiles_root, profile_dir)

        result = export_profile("leaky", str(tmp_path / "leaky.tar.gz"))

        names = _archive_names(result)
        basenames = _archive_basenames(result)

        # Persona survives.
        assert any(n.endswith("config.yaml") for n in names)
        assert any(n.endswith("SOUL.md") for n in names)
        assert any(n.endswith("SKILL.md") for n in names)
        assert any(n.endswith("MEMORY.md") for n in names)
        # Shareable template survives.
        assert any(Path(n).name == ".env.example" for n in names)

        # Every credential root file is gone (exact basename, so .env.example
        # does not trip the .env assertion).
        for name in _CREDENTIAL_ROOT_FILES:
            assert name not in basenames, f"{name} must NOT be in export"

        # Every full credential dir is pruned wholesale: no archive member may
        # carry it below the profile root (parts[0] is the profile name).
        for top in _FULL_DIR_TOPS:
            assert not any(
                top in Path(n).parts[1:] for n in names
            ), f"{top}/ must NOT be in export"

        # cache/ itself may survive (non-credential caches), but its Bitwarden
        # files must not.
        for leaf in ("bws_cache.json", "bws_cache.enc.json"):
            assert leaf not in basenames, f"{leaf} must NOT be in export"

        # Spot-check sensitive leaf basenames nested inside kept parents.
        for leaf in (
            "creds.json", "crypto.db", "Cookies", "server.json",
            "vault.key",
            "google_oauth.json", "slack_tokens.json",
            "webhook_subscriptions.json", "u@x.json",
        ):
            assert leaf not in basenames, f"{leaf} must NOT be in export"

    def test_nested_dotenv_and_oauth_variants_dropped(self, tmp_path, monkeypatch):
        """``.env.*`` variants and ``google_chat_user_*`` stores are stripped at
        any depth; ``.env.example`` / ``.env.template`` templates survive."""
        profiles_root = tmp_path / "profiles"
        profile_dir = profiles_root / "nested"
        profile_dir.mkdir(parents=True)
        (profile_dir / "config.yaml").write_text("model: gpt-4\n", encoding="utf-8")
        nested_skill = profile_dir / "skills" / "demo"
        nested_skill.mkdir(parents=True)
        (nested_skill / "SKILL.md").write_text("# demo\n", encoding="utf-8")
        (nested_skill / ".env.local").write_text("KEY=secret\n", encoding="utf-8")
        (profile_dir / ".env.development").write_text("KEY=secret\n", encoding="utf-8")
        (profile_dir / "google_chat_user_tokens").mkdir()
        (profile_dir / "google_chat_user_tokens" / "someone@example.com.json").write_text(
            "{}", encoding="utf-8"
        )
        _patch_named_profile(monkeypatch, profiles_root, profile_dir)

        result = export_profile("nested", str(tmp_path / "nested.tar.gz"))
        basenames = _archive_basenames(result)
        names = _archive_names(result)

        assert ".env.local" not in basenames
        assert ".env.development" not in basenames
        assert "someone@example.com.json" not in basenames
        assert not any("google_chat_user_tokens" in n for n in names)
        assert any(n.endswith("SKILL.md") for n in names)

    def test_default_export_drops_nested_credentials_in_allowed_dirs(
        self, tmp_path, monkeypatch
    ):
        """Defense in depth: the default-profile allow-list keeps ``skills/``,
        but a credential file nested inside it must still be stripped."""
        profiles_root = tmp_path / "profiles"
        profile_dir = profiles_root / "default-src"
        profile_dir.mkdir(parents=True)
        (profile_dir / "config.yaml").write_text("model: gpt-4\n", encoding="utf-8")
        skill_dir = profile_dir / "skills" / "demo"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("# demo\n", encoding="utf-8")
        (skill_dir / ".env").write_text("KEY=secret\n", encoding="utf-8")
        (skill_dir / "mcp-tokens").mkdir()
        (skill_dir / "mcp-tokens" / "server.json").write_text("{}", encoding="utf-8")
        _patch_named_profile(monkeypatch, profiles_root, profile_dir)
        monkeypatch.setattr(
            "hermes_cli.profiles._get_default_hermes_home", lambda: profiles_root
        )

        # Drive the default-profile branch directly: allow-list + credential
        # exclusion at any depth.
        from hermes_cli import profiles as profiles_mod

        monkeypatch.setattr(
            profiles_mod, "get_profile_dir", lambda n: profile_dir
        )
        result = export_profile("default", str(tmp_path / "default.tar.gz"))

        names = _archive_names(result)
        basenames = _archive_basenames(result)
        assert any(n.endswith("SKILL.md") for n in names)
        assert Path(".env").name not in basenames
        assert "server.json" not in basenames
        assert not any("mcp-tokens" in n for n in names)

    def test_default_export_drops_every_credential_store(self, tmp_path, monkeypatch):
        """Default export excludes every credential store at root and nested
        inside allowed dirs (issue #126114)."""
        profiles_root = tmp_path / "profiles"
        profile_dir = profiles_root / "default-full"
        profile_dir.mkdir(parents=True)
        _seed_credential_profile(profile_dir)
        # Nested copies inside allowed subtrees (skills/, memories/): the
        # allow-list keeps the parent, the credential pass must still prune
        # the secret child at any depth.
        skill_dir = profile_dir / "skills" / "demo"
        for name in (
            ".env", ".op.env", "npmrc", ".anthropic_oauth.json",
            "google_token.json", "slack_tokens.json",
            "webhook_subscriptions.json",
            ".netrc", ".pgpass", ".git-credentials",
        ):
            (skill_dir / name).write_text("secret-material", encoding="utf-8")
        for top, rel in (
            ("mcp-tokens", "server.json"),
            ("vault", "vault.key"),
            ("pairing", "telegram.json"),
            ("browser-profile", "chrome/Default/Cookies"),
        ):
            target = skill_dir / top / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"secret-material")
        _patch_named_profile(monkeypatch, profiles_root, profile_dir)
        monkeypatch.setattr(
            "hermes_cli.profiles._get_default_hermes_home", lambda: profiles_root
        )
        from hermes_cli import profiles as profiles_mod

        monkeypatch.setattr(profiles_mod, "get_profile_dir", lambda n: profile_dir)
        result = export_profile("default", str(tmp_path / "default-full.tar.gz"))

        names = _archive_names(result)
        basenames = _archive_basenames(result)

        # Persona survives.
        assert any(n.endswith("config.yaml") for n in names)
        assert any(n.endswith("SOUL.md") for n in names)
        assert any(n.endswith("SKILL.md") for n in names)
        assert any(n.endswith("MEMORY.md") for n in names)

        # Every credential root file is gone (exact basename so templates
        # do not trip the assertion).
        for name in _CREDENTIAL_ROOT_FILES:
            assert name not in basenames, f"{name} must NOT be in default export"
        # Task-named examples, explicit.
        for name in (
            ".op.env", "npmrc", ".anthropic_oauth.json",
            "google_token.json", "slack_tokens.json",
            "webhook_subscriptions.json",
        ):
            assert name not in basenames, f"{name} must NOT be in default export"

        # Every full credential dir pruned wholesale, at root or nested.
        for top in _FULL_DIR_TOPS:
            assert not any(
                top in Path(n).parts[1:] for n in names
            ), f"{top}/ must NOT be in default export"
        for top in ("vault", "mcp-tokens", "browser-profile", "pairing"):
            assert not any(top in n for n in names), f"{top}/ must NOT be in default export"

        # Sensitive leaves must not leak from any depth.
        for leaf in (
            "creds.json", "crypto.db", "Cookies", "server.json",
            "vault.key", "google_oauth.json", "slack_tokens.json",
            "webhook_subscriptions.json", "u@x.json",
            "bws_cache.json", "bws_cache.enc.json",
        ):
            assert leaf not in basenames, f"{leaf} must NOT be in default export"


class TestExportSecretScrub:

    def test_named_profile_export_redacts_secrets_in_text(self, tmp_path, monkeypatch):
        """Leaked keys in skills / SOUL / memories must not leave the archive."""
        profiles_root = tmp_path / "profiles"
        profile_dir = profiles_root / "scrubme"
        profile_dir.mkdir(parents=True)

        soul = profile_dir / "SOUL.md"
        soul.write_text(f"My key is {_LEAKED_KEY}\n")

        skill_dir = profile_dir / "skills" / "demo"
        skill_dir.mkdir(parents=True)
        skill = skill_dir / "SKILL.md"
        skill.write_text(
            "---\nname: demo\ndescription: Demo.\n---\n"
            f"Use OPENROUTER_API_KEY={_LEAKED_KEY}\n"
        )

        memories = profile_dir / "memories"
        memories.mkdir()
        memory = memories / "MEMORY.md"
        memory.write_text(f"token {_LEAKED_KEY}\n")

        (profile_dir / "config.yaml").write_text("model: gpt-4\n")

        _patch_named_profile(monkeypatch, profiles_root, profile_dir)

        result = export_profile("scrubme", str(tmp_path / "scrubme.tar.gz"))

        with tarfile.open(result, "r:gz") as tf:
            members = {
                name: tf.extractfile(name).read().decode("utf-8")
                for name in tf.getnames()
                if name.endswith((".md", ".yaml"))
            }

        blob = "\n".join(members.values())
        assert _LEAKED_KEY not in blob
        assert any("SOUL.md" in n for n in members)
        assert any("SKILL.md" in n for n in members)
        assert any("MEMORY.md" in n for n in members)

        # Live profile must keep the original plaintext.
        assert _LEAKED_KEY in soul.read_text()
        assert _LEAKED_KEY in skill.read_text()
        assert _LEAKED_KEY in memory.read_text()

    @pytest.mark.require_symlinks
    def test_export_redacts_through_symlink_without_touching_source(
        self, tmp_path, monkeypatch
    ):
        """Symlinked skill text is redacted in the archive, source file stays put."""
        profiles_root = tmp_path / "profiles"
        profile_dir = profiles_root / "linkme"
        profile_dir.mkdir(parents=True)

        outside = tmp_path / "outside-skill.md"
        outside.write_text(f"secret {_LEAKED_KEY}\n")

        skill_dir = profile_dir / "skills" / "linked"
        skill_dir.mkdir(parents=True)
        link = skill_dir / "SKILL.md"
        link.symlink_to(outside)

        (profile_dir / "config.yaml").write_text("model: gpt-4\n")
        _patch_named_profile(monkeypatch, profiles_root, profile_dir)

        result = export_profile("linkme", str(tmp_path / "linkme.tar.gz"))

        with tarfile.open(result, "r:gz") as tf:
            skill_members = [n for n in tf.getnames() if n.endswith("SKILL.md")]
            assert skill_members
            archived = tf.extractfile(skill_members[0]).read().decode("utf-8")

        assert _LEAKED_KEY not in archived
        assert _LEAKED_KEY in outside.read_text()
        assert link.is_symlink()
