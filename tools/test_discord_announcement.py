"""单元测试：验证 Discord 官方公告抓取与解析引擎 (tools/test_discord_announcement.py)"""

import unittest
from pathlib import Path

from tools import discord_announcement

SAMPLE_DISCORD_PATCH_NOTES = """
【維護時間】
11/26 14:00 - 18:00 (UTC+8)

【新增項目】
1. 開啟全新限定活動「正義的誓約」活動故事與活動關卡
2. 開啟限定活動招募：5★ 團員「執法者維爾莉特」與 5★ 專屬羈絆「守護誓約」
3. 開啟神秘招募：5★ 團員「諾雅爾」與 5★ 專屬羈絆「特別診斷」
4. 復刻活動招募：5★ 團員「雪江」

【修復項目】
- 修復部分介面顯示異常
"""

class TestDiscordAnnouncementParser(unittest.TestCase):
    def test_parse_maintenance_window(self):
        window = discord_announcement.parse_maintenance_time(SAMPLE_DISCORD_PATCH_NOTES, default_year=2026)
        self.assertIsNotNone(window)
        self.assertIn("11/26 14:00 - 18:00", window["raw"])
        self.assertIn("2026-11-26T14:00:00+08:00", window["start"])
        self.assertIn("2026-11-26T18:00:00+08:00", window["end"])

    def test_parse_pickup_and_mystic_heroes(self):
        res = discord_announcement.parse_announcement_text(SAMPLE_DISCORD_PATCH_NOTES)
        self.assertIsNotNone(res)
        self.assertIn("限定活動「正義的誓約」", res["title"])

        # 验证限定团员提取
        pickup_ids = [h["id"] for h in res["pickup_heroes"]]
        self.assertIn("H198", pickup_ids)  # 執法者維爾莉特

        # 验证神秘团员提取（彻底解决神秘招募不发公告问题）
        mystic_ids = [h["id"] for h in res["mystic_heroes"]]
        self.assertIn("H191", mystic_ids)  # 諾雅爾

        # 验证复刻团员提取
        rerun_ids = [h["id"] for h in res["rerun_heroes"]]
        self.assertIn("H188", rerun_ids)  # 雪江

if __name__ == "__main__":
    unittest.main()
