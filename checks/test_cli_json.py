#!/usr/bin/env python3
"""Contract tests for the engineering-gate JSON adapter."""

import contextlib
import io
import json
import unittest

import cli


class TestCliJson(unittest.TestCase):
    def test_one_json_document_contains_gate_messages_and_summary(self):
        output = io.StringIO()

        def fake_gate():
            print("FAIL: demo.py:3 bad construct")
            return 1

        with contextlib.redirect_stdout(output):
            code = cli._aggregate([("demo", fake_gate)], json_mode=True)
        payload = json.loads(output.getvalue())
        self.assertEqual(code, 1)
        self.assertEqual(payload["demo"], ["FAIL: demo.py:3 bad construct"])
        self.assertEqual(payload["_summary"]["failed"], 1)

    def test_json_success_has_empty_gate_array(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(cli._aggregate([("demo", lambda: 0)], True), 0)
        self.assertEqual(json.loads(output.getvalue()),
                         {"_summary": {"failed": 0}, "demo": []})


if __name__ == "__main__":
    unittest.main()
