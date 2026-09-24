from types import SimpleNamespace
import unittest

import numpy as np

from src.ConfigManager import ConfigManager
from src.handlers.ErrorAnomalyHandler import ErrorAnomalyHandler
from src.utils.anomaly_type import AnomalyType, Protocol, PROTOCOL_SPEC
from src.utils.config_schema import AnomalyKey
from src.utils.shared_data import event_dtype


class ErrorRuleConfigTests(unittest.TestCase):
    def setUp(self):
        self.manager = ConfigManager.__new__(ConfigManager)
        self.key = AnomalyKey(Protocol.SMB, AnomalyType.ERROR)
        self.axes = PROTOCOL_SPEC[Protocol.SMB][AnomalyType.ERROR]["smbiosnoop"]

    def test_rules_build_union_filter_for_single_tracer(self):
        track = self.manager._get_error_track_cmds(
            {
                "rules": [
                    {
                        "name": "severe",
                        "acceptable_count": 3,
                        "track_errors": ["STATUS_ACCESS_DENIED"],
                    },
                    {
                        "name": "missing-create-burst",
                        "acceptable_count": 100,
                        "track_commands": ["SMB2_CREATE"],
                        "track_errors": ["STATUS_OBJECT_NAME_NOT_FOUND"],
                    },
                ]
            },
            self.axes,
            self.key,
        )

        self.assertEqual(track["track_commands"], frozenset())
        self.assertEqual(
            track["track_errors"], frozenset({0xC0000022, 0xC0000034})
        )
        self.assertEqual(len(track["rules"]), 2)

    def test_rules_cannot_mix_with_flat_filters(self):
        with self.assertRaisesRegex(ValueError, "cannot combine"):
            self.manager._get_error_track_cmds(
                {
                    "track_errors": ["STATUS_ACCESS_DENIED"],
                    "rules": [
                        {
                            "name": "severe",
                            "track_errors": ["STATUS_ACCESS_DENIED"],
                        }
                    ],
                },
                self.axes,
                self.key,
            )

    def test_io_rule_resolves_syscall_and_errno_names(self):
        key = AnomalyKey(Protocol.IO, AnomalyType.ERROR)
        axes = PROTOCOL_SPEC[Protocol.IO][AnomalyType.ERROR]["iosnoop"]

        track = self.manager._get_error_track_cmds(
            {
                "rules": [
                    {
                        "name": "failed-writes",
                        "acceptable_count": 3,
                        "track_commands": ["WRITE"],
                        "track_errors": ["EIO", "ENOSPC"],
                    }
                ]
            },
            axes,
            key,
        )

        self.assertEqual(track["track_commands"], frozenset({4}))
        self.assertEqual(track["track_errors"], frozenset({5, 28}))


class ErrorRuleHandlerTests(unittest.TestCase):
    @staticmethod
    def _batch(commands, errors):
        batch = np.zeros(len(commands), dtype=event_dtype)
        batch["command"] = commands
        batch["metric_latency_ns"] = np.asarray(errors, dtype=np.uint64)
        return batch

    def setUp(self):
        self.handler = ErrorAnomalyHandler(
            SimpleNamespace(
                key=AnomalyKey(Protocol.SMB, AnomalyType.ERROR),
                tool="smbiosnoop",
                acceptable_count=1,
                track={
                    "rules": (
                        {
                            "name": "severe",
                            "acceptable_count": 3,
                            "track_commands": frozenset(),
                            "track_errors": frozenset({0xC0000022}),
                        },
                        {
                            "name": "missing-create-burst",
                            "acceptable_count": 100,
                            "track_commands": frozenset({5}),
                            "track_errors": frozenset({0xC0000034}),
                        },
                    )
                },
            )
        )

    def test_severe_rule_uses_its_lower_threshold(self):
        batch = self._batch([5, 8, 9], [0xC0000022] * 3)
        self.assertTrue(self.handler.detect(batch))

    def test_expected_error_only_counts_for_matching_command(self):
        batch = self._batch([5] * 99 + [8] * 10, [0xC0000034] * 109)
        self.assertFalse(self.handler.detect(batch))

        batch = self._batch([5] * 100, [0xC0000034] * 100)
        self.assertTrue(self.handler.detect(batch))


if __name__ == "__main__":
    unittest.main()