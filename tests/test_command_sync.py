import asyncio
import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, PropertyMock, patch

import discord
from database import Database


# Import the real command definitions without connecting to PostgreSQL or Discord.
spec = importlib.util.spec_from_file_location(
    "pfp_main_under_test", Path(__file__).resolve().parents[1] / "main.py"
)
main = importlib.util.module_from_spec(spec)
with patch.dict(os.environ, {"DATABASE_URL": "postgresql://test/test", "GUILD_ID": "old-server"}), patch.object(Database, "_init_schema"):
    spec.loader.exec_module(main)


class CommandSyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = main.PFPBot()
        self.bot.tree.add_command(main.pfp)
        self.bot.tree.sync = AsyncMock(return_value=[main.pfp])
        self.old_guild = SimpleNamespace(id=1483821302463860798)
        self.new_guild = SimpleNamespace(id=222222222222222222)
        self.bot_patch = patch.object(main, "bot", self.bot)
        self.bot_patch.start()

    async def asyncTearDown(self):
        self.bot_patch.stop()
        await self.bot.close()

    async def run_ready(self, guilds):
        with patch.object(main.PFPBot, "guilds", new_callable=PropertyMock, return_value=guilds):
            await main.on_ready()

    async def test_setup_does_not_sync_before_guilds_are_known(self):
        await self.bot.setup_hook()
        self.bot.tree.sync.assert_not_awaited()
        self.assertEqual(len(self.bot.persistent_views), 1)

    async def test_ready_registers_real_pfp_commands_in_current_servers(self):
        await self.run_ready([self.old_guild, self.new_guild])
        self.assertEqual(self.bot.tree.sync.await_count, 2)
        self.bot.tree.sync.assert_any_await(guild=self.new_guild)
        registered = self.bot.tree.get_command("pfp", guild=self.new_guild)
        self.assertIs(registered, main.pfp)
        self.assertTrue({"new", "suggest", "suggestions", "random", "close", "voters"}.issubset(
            {command.name for command in registered.commands}
        ))

    async def test_reconnect_does_not_repeat_successful_registration(self):
        await self.run_ready([self.new_guild])
        await self.run_ready([self.new_guild])
        self.bot.tree.sync.assert_awaited_once_with(guild=self.new_guild)

    async def test_join_registers_without_restart(self):
        await main.on_guild_join(self.new_guild)
        self.bot.tree.sync.assert_awaited_once_with(guild=self.new_guild)

    async def test_rejoin_registers_again(self):
        await main.on_guild_join(self.new_guild)
        await main.on_guild_remove(self.new_guild)
        await main.on_guild_join(self.new_guild)
        self.assertEqual(self.bot.tree.sync.await_count, 2)

    async def test_one_failure_does_not_block_other_servers_and_can_retry(self):
        error = discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "Missing access")
        self.bot.tree.sync.side_effect = [error, [main.pfp], [main.pfp]]
        with self.assertLogs(main.logger, level="ERROR"):
            await self.run_ready([self.old_guild, self.new_guild])
        self.assertNotIn(self.old_guild.id, self.bot._synced_guild_ids)
        self.assertIn(self.new_guild.id, self.bot._synced_guild_ids)
        await self.run_ready([self.old_guild, self.new_guild])
        self.assertEqual(self.bot.tree.sync.await_count, 3)

    async def test_simultaneous_ready_and_join_do_not_duplicate_sync(self):
        await asyncio.gather(
            self.run_ready([self.new_guild]),
            main.on_guild_join(self.new_guild),
        )
        self.bot.tree.sync.assert_awaited_once_with(guild=self.new_guild)


if __name__ == "__main__":
    unittest.main()
