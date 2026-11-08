import unittest
from unittest.mock import Mock, patch

import run_demo


class DemoLauncherTests(unittest.TestCase):
    def test_1200_flights_60x_and_frontends_are_stopped(self):
        children = [Mock(), Mock()]
        with patch.object(run_demo, "start_frontend", side_effect=children) as start, \
             patch.object(run_demo, "stop_frontend") as stop, \
             patch.object(run_demo.run_local, "main", return_value=0) as backend:
            self.assertEqual(run_demo.main(), 0)
        self.assertEqual(start.call_args_list[0].args, ("operator", 5173))
        self.assertEqual(start.call_args_list[1].args, ("passenger", 5174))
        self.assertEqual(backend.call_args.args[0][-4:], ["--mode", "paced", "--speed", "60"])
        self.assertTrue(backend.call_args.args[0][1].endswith("pearson-2026-11-07-seed42.json"))
        self.assertEqual([call.args[0] for call in stop.call_args_list], children[::-1])
