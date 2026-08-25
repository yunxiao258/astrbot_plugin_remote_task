# -*- coding: utf-8 -*-
"""remote_task listen_loop 退避与日志降级测试（v1.2.2）。"""

import asyncio
import sys
import unittest
from unittest import mock

sys.path.insert(0, r"D:\astrbot\data\plugins")

from astrbot_plugin_remote_task.client import ServeClient  # noqa: E402


def make_client():
    try:
        return ServeClient("http://127.0.0.1:4096", token="")
    except TypeError:
        return ServeClient("http://127.0.0.1:4096")


class TestListenLoopBackoff(unittest.TestCase):
    def _run_loop_briefly(self, client, fail_times, sleep_calls):
        """让 _listen_once 连续失败 fail_times 次后抛 CancelledError 退出，
        记录每次 sleep 的时长。"""

        async def fake_sleep(sec):
            sleep_calls.append(sec)
            if len(sleep_calls) >= fail_times:
                raise asyncio.CancelledError()

        def failing_once(on_event, loop=None):
            # to_thread 需要同步函数
            raise ConnectionError("refused")

        client._listen_once = failing_once
        with mock.patch("asyncio.sleep", side_effect=fake_sleep):
            try:
                asyncio.run(client.listen_loop(lambda *a: None, loop=None))
            except asyncio.CancelledError:
                pass

    def test_backoff_caps_at_300(self):
        client = make_client()
        sleeps = []
        self._run_loop_briefly(client, fail_times=8, sleep_calls=sleeps)
        # 首次失败即翻倍：10, 20, 40, 80, 160, 300, 300, 300
        self.assertEqual(sleeps, [10, 20, 40, 80, 160, 300, 300, 300])

    def test_warn_only_first_and_every_10th(self):
        client = make_client()
        warnings = []
        with mock.patch("astrbot_plugin_remote_task.client.logger") as fake_log:
            fake_log.warning.side_effect = lambda msg: warnings.append(msg)
            fake_log.debug = lambda msg: None
            fake_log.info = lambda msg: None
            sleeps = []
            self._run_loop_briefly(client, fail_times=25, sleep_calls=sleeps)
        # 第 1 次与第 10/20 次 WARN，共 3 条；其余 DEBUG
        self.assertEqual(len(warnings), 3, f"warnings={warnings}")
        self.assertIn("第 1 次", warnings[0])
        self.assertIn("第 10 次", warnings[1])
        self.assertIn("第 20 次", warnings[2])


if __name__ == "__main__":
    unittest.main(verbosity=1)
