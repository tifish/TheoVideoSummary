"""离线回归：响应捕获、长字幕完整性、限流停止及标签页归属。"""
import json
import shutil
import subprocess
import unittest
from unittest.mock import patch

from browser_transcript import BrowserSession, _INSTALL_CAPTURE_JS
from fetch import Blocked, fetch_transcript


@unittest.skipUnless(shutil.which("node"), "Node.js required")
class CaptureTests(unittest.TestCase):
    def capture(self, status, body):
        script = r"""
const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));
let paused = 0;
global.window = global;
global.location = {href:'https://www.youtube.com/watch?v=nYA0yASgaZI'};
global.document = {querySelector:()=>({pauseVideo:()=>paused++}),
  querySelectorAll:()=>[],addEventListener:()=>{},documentElement:{}};
global.MutationObserver = class {observe(){}};
global.XMLHttpRequest = class {open(){}};
global.fetch = async () => ({url:'https://www.youtube.com/api/timedtext?v=nYA0yASgaZI&lang=en',
  status:input.status,clone:()=>({text:async()=>input.body})});
""" + "(" + _INSTALL_CAPTURE_JS + ")('nYA0yASgaZI');" + r"""
fetch('unused').then(()=>setImmediate(()=>console.log(JSON.stringify({
  ...window.__theoCaptions,paused
}))));
"""
        result = subprocess.run(["node", "-e", script],
                                input=json.dumps({"status": status, "body": body}),
                                text=True, encoding="utf-8", capture_output=True, check=True)
        return json.loads(result.stdout)

    def test_full_large_unicode_capture_and_chunk_transfer(self):
        data = {"events": [{"tStartMs": i * 1000, "segs": [{"utf8": "字幕😀\\\"\n" * 30}]}
                           for i in range(1874)]}
        capture = self.capture(200, json.dumps(data))
        self.assertIsNone(capture["error"])
        self.assertGreater(len(capture["serialized"]), 75000)
        self.assertTrue(capture["serialized"].isascii())
        self.assertEqual(capture["paused"], 1)
        b = BrowserSession()
        b._eval = lambda _fn, args: capture["serialized"][args[0]:args[0]+args[1]]
        status = {**capture, "length": len(capture["serialized"])}
        self.assertEqual(b._read_capture(status), data)
        status["eventCount"] += 1
        with self.assertRaisesRegex(RuntimeError, "integrity"):
            b._read_capture(status)

    def test_rate_limit_and_empty_response_pause_without_retry(self):
        for status, body, error in [(429, "rate limited", "429"), (200, "", "EMPTY_CAPTIONS")]:
            with self.subTest(status=status):
                capture = self.capture(status, body)
                self.assertIn(error, capture["error"])
                self.assertEqual(capture["paused"], 1)
                b = BrowserSession()
                b._open = lambda _id: {"status": "OK", "tracks": [{"lang": "en", "kind": "asr"}]}
                b._eval = lambda *_: capture
                with patch.object(b, "_close_page") as close, patch.object(b, "_open", wraps=b._open) as opened:
                    with self.assertRaises(Blocked):
                        fetch_transcript("nYA0yASgaZI", b)
                    self.assertEqual(opened.call_count, 1)
                    close.assert_called_once()


class SessionTests(unittest.TestCase):
    def test_cleanup_closes_only_owned_tabs(self):
        b = BrowserSession()
        b._tabs = {"101", "102"}
        b._page = "101"
        commands = []
        b._command = commands.append
        from unittest.mock import Mock
        b._mcp = Mock()
        client = b._mcp
        b.close()
        self.assertEqual(set(commands), {"tab close 101", "tab close 102"})
        client.close.assert_called_once()

    def test_truncated_output_is_rejected(self):
        b = BrowserSession()
        b._page = "101"
        b._command = lambda _: '✓ tab select 101\n{"theoResult":"unfinished'
        with self.assertRaisesRegex(RuntimeError, "truncated"):
            b._eval("() => 'anything'")


if __name__ == "__main__":
    unittest.main()
