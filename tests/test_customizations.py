import json
import unittest
import tempfile
import subprocess
from pathlib import Path
from unittest.mock import patch

from claude_launcher.customizations import (
    SKILL_SETTINGS, skills_arguments, project_instruction_mounts, shared_skill_mounts,
)
from claude_launcher.wire import RouteError


class CustomizationTests(unittest.TestCase):
    def test_setup_token_reaches_official_subcommand_without_variadic_mcp_flags(self):
        self.assertEqual(skills_arguments(["setup-token"]), ["--safe-mode", "setup-token"])
        self.assertEqual(skills_arguments(["setup-token", "--help"]),
                         ["--safe-mode", "setup-token", "--help"])

    def test_shared_skills_keep_private_priority_and_reject_external_links(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            profile = home / "profile"
            private = profile / ".claude/skills"
            universal = home / ".agents/skills"
            personal = home / ".claude/skills"
            for root in (private, universal, personal):
                root.mkdir(parents=True)
            for root, name in ((private, "existing"), (universal, "existing"),
                               (universal, "orca-cli"), (personal, "personal"),
                               (universal, "synced")):
                (root / name).mkdir()
                (root / name / "SKILL.md").write_text("Fixture skill")
            (personal / "orca-cli").symlink_to(universal / "orca-cli")
            outside = home / "unrelated"
            outside.mkdir()
            (outside / "SKILL.md").write_text("Not shared")
            (universal / "external").symlink_to(outside)
            bundled = universal / "bundle"
            bundled.mkdir()
            (bundled / "SKILL.md").write_text("Plugin bundle")
            (bundled / ".claude-plugin").mkdir()
            mounts = shared_skill_mounts(profile, home)
            self.assertIn(str(private / "existing"), mounts)
            self.assertNotIn(str(universal / "existing"), mounts)
            self.assertEqual(mounts.count(str(universal / "orca-cli")), 1)
            self.assertIn(str(personal / "personal"), mounts)
            self.assertNotIn(str(outside), mounts)
            self.assertNotIn(str(bundled), mounts)
            self.assertNotIn(str(universal / "synced"), mounts)

    def test_real_shared_skills_are_readonly_and_future_installs_are_discovered(self):
        from claude_launcher.runner import sandbox_command
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            profile = home / ".local/share/claude-launcher/profiles/fixture"
            profile.mkdir(parents=True)
            project = home / "project"
            project.mkdir()
            skills = home / ".agents/skills"
            (skills / "first/references").mkdir(parents=True)
            (skills / "first/SKILL.md").write_text("Fixture first")
            (skills / "first/references/guide.md").write_text("Relative reference")
            (home / ".agents/private-state").write_text("Unrelated state")
            (home / ".claude").mkdir()
            (home / ".claude/.credentials.json").write_text("Synthetic credential")
            upstream = home / "unused.sock"
            upstream.touch()
            for names in (["first"], ["first", "next"]):
                if "next" in names:
                    (skills / "next").mkdir()
                    (skills / "next/SKILL.md").write_text("Fixture next")
                code = f'''
import os
from pathlib import Path
home, profile = Path({str(home)!r}), Path({str(profile)!r})
skills = profile/'.claude/skills'
assert sorted(p.name for p in skills.iterdir()) == {names!r}
assert (skills/'first/references/guide.md').read_text() == 'Relative reference'
assert os.statvfs(skills/'first').f_flag & os.ST_RDONLY
assert not (home/'.agents/private-state').exists()
assert not (home/'.claude/.credentials.json').exists()
assert len(Path('/proc/net/route').read_text().splitlines()) == 1
'''
                with patch('claude_launcher.runner.Path.home', return_value=home):
                    args, env = sandbox_command(upstream, profile, project,
                                                ['/usr/bin/python3', '-c', code],
                                                research_filesystem=True,
                                                project_instructions=True)
                result = subprocess.run(args, env=env, capture_output=True,
                                        text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((profile / '.claude/skills/first').exists())
            self.assertEqual((home / '.claude/.credentials.json').read_text(),
                             'Synthetic credential')

    def test_user_only_skills_without_project_integrations(self):
        args = skills_arguments(["--model", "sonnet"])
        self.assertEqual(args[:2], ["--setting-sources", "user,project,local"])
        self.assertEqual(json.loads(args[args.index("--settings") + 1]), SKILL_SETTINGS)
        self.assertIn("--strict-mcp-config", args)
        self.assertEqual(json.loads(args[args.index("--mcp-config") + 1]), {"mcpServers": {}})
        self.assertNotIn("--safe-mode", args)
        self.assertTrue(SKILL_SETTINGS["disableAllHooks"])
        self.assertTrue(SKILL_SETTINGS["disableClaudeAiConnectors"])
        self.assertTrue(SKILL_SETTINGS["autoMemoryEnabled"])
        self.assertTrue(SKILL_SETTINGS["disableRemoteControl"])
        self.assertNotIn("claudeMdExcludes", SKILL_SETTINGS)
        self.assertFalse(SKILL_SETTINGS["syncClaudeAiSkills"])

    def test_loading_overrides_cannot_reenable_old_integrations(self):
        for flag in ("--settings", "--setting-sources", "--plugin-dir", "--mcp-config",
                     "--plugin-url", "--agents", "--remote-control", "--rc"):
            for argument in (flag, flag + "=fixture"):
                with self.assertRaises(RouteError):
                    skills_arguments([argument])

    def test_remote_control_subcommand_disabled(self):
        for command in ("rc", "remote-control"):
            with self.assertRaises(RouteError):
                skills_arguments([command])

    def test_project_view_keeps_rules_but_not_settings_skills_agents(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            local = root / ".claude"
            local.mkdir()
            for name in ("rules", "agents", "skills"):
                (local / name).mkdir()
            (local / "CLAUDE.md").write_text("Fixture")
            settings = local / "settings.json"
            settings.write_text('{"env":{"HTTPS_PROXY":"old"}}')
            args = project_instruction_mounts(root)
            self.assertEqual(args[:2], ["--tmpfs", str(local)])
            self.assertIn(str(local / "rules"), args)
            self.assertNotIn(str(local / "agents"), args)
            self.assertIn(str(local / "CLAUDE.md"), args)
            self.assertNotIn(str(settings), args)
            self.assertNotIn(str(local / "skills"), args)
            self.assertEqual(settings.read_text(), '{"env":{"HTTPS_PROXY":"old"}}')

    def test_real_project_view_hides_settings_and_preserves_instruction_files(self):
        from claude_launcher.runner import sandbox_command
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            profile = root / "profile"
            profile.mkdir()
            project = root / "project"
            for prefix in (project, project / "nested"):
                directory = prefix / ".claude"
                for name in ("rules", "agents", "skills"):
                    (directory / name).mkdir(parents=True)
                (directory / "rules/rule.md").write_text("Fixture rule")
                (directory / "agents/agent.md").write_text("Fixture agent")
                (directory / "settings.json").write_text('{"env":{"HTTPS_PROXY":"old"}}')
                (directory / "skills/old.md").write_text("Old skill")
            upstream = root / "unused.sock"
            upstream.touch()
            code = (
                "from pathlib import Path; "
                "roots=[Path('.claude'),Path('nested/.claude')]; "
                "assert all((p/'rules/rule.md').read_text()=='Fixture rule' for p in roots); "
                "assert all(not (p/'agents').exists() for p in roots); "
                "assert all(not (p/'settings.json').exists() for p in roots); "
                "assert all(not (p/'skills').exists() for p in roots)"
            )
            args, env = sandbox_command(upstream, profile, project,
                                        ["/usr/bin/python3", "-c", code],
                                        project_instructions=True)
            result = subprocess.run(args, env=env, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((project / ".claude/settings.json").exists())
            self.assertTrue((project / "nested/.claude/skills/old.md").exists())
