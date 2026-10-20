# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Henk Duisterwinkel
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from basicagentpy.tools.web import (
    _pinned_address,
    decode_entities,
    detect_charset,
    domain_matches,
    html_to_text,
    is_private_address,
    web_tools,
)


def web_config(overrides=None):
    config = {
        "tools": {
            "web": {
                "enabled": True,
                "approval": "never",
                "timeoutMs": 5000,
                "maxRawBytes": 512000,
                "maxBytes": 24000,
                "maxRedirects": 5,
                "allowPrivate": True,
                "denyDomains": [],
                "allowDomains": [],
                "userAgent": "",
            }
        }
    }
    config["tools"]["web"].update(overrides or {})
    return config


def tool(config=None):
    return web_tools(config=config or web_config())[0]


def serve(callback):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def log_message(self, format, *args):
            return

        def do_GET(self):  # noqa: N802
            try:
                callback(self)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{server.server_address[1]}"

    class Server:
        def close(self):
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    result = Server()
    result.origin = origin
    return result


def html_response(handler, body, content_type="text/html; charset=utf-8"):
    handler.send_response(200)
    handler.send_header("content-type", content_type)
    handler.end_headers()
    handler.wfile.write(body.encode("utf-8"))


CTX = {"approve": lambda description: True}


class WebToolTests(unittest.TestCase):
    def server(self, callback):
        server = serve(callback)
        self.addCleanup(server.close)
        return server

    def test_the_tool_is_omitted_entirely_when_disabled(self):
        self.assertEqual(web_tools(config=web_config({"enabled": False})), [])

    def test_fetches_and_sanitizes_html(self):
        def callback(handler):
            html_response(
                handler,
                (
                    "<!doctype html><html><head><title>t</title><script>alert(1)</script>"
                    "<style>body{color:red}</style></head><body>"
                    "<nav>navigation</nav>"
                    "<main><h1>Heading</h1><p>First &amp; second.</p>"
                    "<p>Entity: &amp;lt; stays, &#233; decodes.</p>"
                    "<ul><li>one</li><li>two</li></ul></main>"
                    "<footer>footer text</footer></body></html>"
                ),
            )

        server = self.server(callback)
        result = tool().run({"url": server.origin + "/"}, CTX)
        self.assertEqual(result["status"], 200)
        self.assertRegex(result["text"], r"# Heading")
        self.assertRegex(result["text"], r"First & second\.")
        self.assertRegex(result["text"], r"Entity: &lt; stays, é decodes\.")
        self.assertRegex(result["text"], r"- one\n- two")
        self.assertNotIn("navigation", result["text"])
        self.assertNotIn("footer text", result["text"])
        self.assertNotIn("alert(1)", result["text"])
        self.assertNotIn("color:red", result["text"])
        self.assertFalse(result["truncated"])

    def test_returns_plain_text_and_json_unchanged(self):
        def callback(handler):
            if handler.path == "/plain":
                handler.send_response(200)
                handler.send_header("content-type", "text/plain")
                handler.end_headers()
                handler.wfile.write(b"just text")
                return
            handler.send_response(200)
            handler.send_header("content-type", "application/json")
            handler.end_headers()
            handler.wfile.write(b'{"a": 1}')

        server = self.server(callback)
        plain = tool().run({"url": server.origin + "/plain"}, CTX)
        self.assertEqual(plain["text"], "just text")
        data = tool().run({"url": server.origin + "/json"}, CTX)
        self.assertEqual(data["text"], '{"a": 1}')

    def test_decodes_iso_8859_1_from_the_header_and_windows_1252_from_the_meta_tag(self):
        def callback(handler):
            if handler.path == "/header":
                handler.send_response(200)
                handler.send_header("content-type", "text/html; charset=iso-8859-1")
                handler.end_headers()
                handler.wfile.write(b"<p>caf" + bytes([0xE9]) + b"</p>")
                return
            handler.send_response(200)
            handler.send_header("content-type", "text/html")
            handler.end_headers()
            handler.wfile.write(
                b'<html><body><meta charset="windows-1252"><p>quote'
                + bytes([0x92])
                + b"</p></body></html>"
            )

        server = self.server(callback)
        header = tool().run({"url": server.origin + "/header"}, CTX)
        self.assertIn("café", header["text"])
        meta = tool().run({"url": server.origin + "/meta"}, CTX)
        self.assertIn("quote\u2019", meta["text"])

    def test_truncates_the_sanitized_text_and_flags_it(self):
        server = self.server(
            lambda handler: html_response(handler, "<p>" + "word " * 500 + "</p>")
        )
        result = tool(web_config({"maxBytes": 120})).run(
            {"url": server.origin + "/"}, CTX
        )
        self.assertTrue(result["truncated"])
        self.assertLessEqual(len(result["text"].encode("utf-8")), 120)

    def test_truncates_huge_raw_bodies_before_sanitizing(self):
        server = self.server(
            lambda handler: html_response(handler, "<p>" + "x" * 50000 + "</p>")
        )
        result = tool(web_config({"maxRawBytes": 200})).run(
            {"url": server.origin + "/"}, CTX
        )
        self.assertTrue(result["truncated"])

    def test_follows_redirects_and_reports_the_final_url(self):
        def callback(handler):
            if handler.path == "/a":
                handler.send_response(302)
                handler.send_header("location", "/b")
                handler.end_headers()
                return
            html_response(handler, "<p>arrived</p>")

        server = self.server(callback)
        result = tool().run({"url": server.origin + "/a"}, CTX)
        self.assertIn("arrived", result["text"])
        self.assertEqual(result["finalUrl"], server.origin + "/b")

    def test_refuses_too_many_redirects(self):
        def callback(handler):
            step = int(handler.path[1:] or "0")
            handler.send_response(302)
            handler.send_header("location", f"/{step + 1}")
            handler.end_headers()

        server = self.server(callback)
        with self.assertRaisesRegex(ValueError, "Too many redirects"):
            tool(web_config({"maxRedirects": 2})).run(
                {"url": server.origin + "/0"}, CTX
            )

    def test_rechecks_domain_policy_on_redirect_targets(self):
        def callback(handler):
            handler.send_response(302)
            handler.send_header("location", "http://blocked.example/")
            handler.end_headers()

        server = self.server(callback)
        with self.assertRaisesRegex(ValueError, "deny list"):
            tool(web_config({"denyDomains": ["blocked.example"]})).run(
                {"url": server.origin + "/"}, CTX
            )

    def test_blocks_private_and_reserved_addresses_by_default(self):
        blocked = tool(web_config({"allowPrivate": False}))
        with self.assertRaisesRegex(ValueError, "private or reserved"):
            blocked.run({"url": "http://127.0.0.1:9/"}, CTX)
        with self.assertRaisesRegex(ValueError, "private or reserved"):
            blocked.run({"url": "http://[::1]:9/"}, CTX)
        with self.assertRaisesRegex(ValueError, "private or reserved"):
            blocked.run({"url": "http://169.254.169.254/latest/meta-data/"}, CTX)
        with self.assertRaisesRegex(ValueError, "Blocked host"):
            blocked.run({"url": "http://localhost:9/"}, CTX)

    def test_domain_allow_and_deny_lists_work_without_network_access(self):
        with self.assertRaisesRegex(ValueError, "deny list"):
            tool(web_config({"allowPrivate": False, "denyDomains": ["example.com"]})).run(
                {"url": "http://example.com/"}, CTX
            )
        with self.assertRaisesRegex(ValueError, "allow list"):
            tool(web_config({"allowDomains": ["only.test"]})).run(
                {"url": "http://example.com/"}, CTX
            )

    def test_rejects_unsupported_content_types_and_http_errors(self):
        def callback(handler):
            if handler.path == "/blob":
                handler.send_response(200)
                handler.send_header("content-type", "application/octet-stream")
                handler.end_headers()
                handler.wfile.write(b"binary")
                return
            handler.send_response(404)
            handler.send_header("content-type", "text/plain")
            handler.end_headers()
            handler.wfile.write(b"nope")

        server = self.server(callback)
        with self.assertRaisesRegex(ValueError, "Unsupported content type"):
            tool().run({"url": server.origin + "/blob"}, CTX)
        with self.assertRaisesRegex(ValueError, "HTTP 404"):
            tool().run({"url": server.origin + "/missing"}, CTX)

    def test_times_out_and_leaves_no_hanging_request(self):
        def callback(handler):
            time.sleep(0.5)
            html_response(handler, "<p>late</p>")

        server = self.server(callback)
        with self.assertRaisesRegex(ValueError, "timed out"):
            tool(web_config({"timeoutMs": 80})).run(
                {"url": server.origin + "/"}, CTX
            )

    def test_approval_always_can_deny_a_fetch(self):
        with self.assertRaisesRegex(ValueError, "denied by user"):
            tool(web_config({"approval": "always"})).run(
                {"url": "http://example.com/"},
                {"approve": lambda description: False},
            )

    def test_rejects_malformed_urls(self):
        with self.assertRaisesRegex(ValueError, r"Only http\(s\)"):
            tool().run({"url": "file:///etc/passwd"}, CTX)
        with self.assertRaisesRegex(ValueError, "credentials"):
            tool().run({"url": "http://user:pass@example.com/"}, CTX)
        with self.assertRaisesRegex(ValueError, 'non-empty "url"'):
            tool().run({"url": ""}, CTX)

    def test_is_private_address_covers_the_important_ranges(self):
        for ip in [
            "127.0.0.1",
            "0.0.0.0",
            "10.1.2.3",
            "172.16.0.1",
            "172.31.255.255",
            "192.168.1.1",
            "169.254.169.254",
            "100.64.0.1",
            "224.0.0.1",
            "::1",
            "::",
            "fc00::1",
            "fd12::1",
            "fe80::1",
            "ff02::1",
            "::ffff:127.0.0.1",
        ]:
            self.assertTrue(is_private_address(ip), f"{ip} should be blocked")
        for ip in [
            "8.8.8.8",
            "1.1.1.1",
            "172.32.0.1",
            "192.169.0.1",
            "2001:4860:4860::8888",
            "::ffff:8.8.8.8",
        ]:
            self.assertFalse(is_private_address(ip), f"{ip} should be allowed")

    def test_is_private_address_blocks_non_canonical_and_v4_in_v6_forms(self):
        for ip in [
            "0:0:0:0:0:0:0:1",
            "0:0:0:0:0:0:0:0",
            "::7f00:1",
            "::ffff:0:127.0.0.1",
            "64:ff9b::7f00:1",
            "2002:7f00:1::",
            "2001::1",
            "100::1",
            "2001:db8::1",
        ]:
            self.assertTrue(is_private_address(ip), f"{ip} should be blocked")
        self.assertTrue(is_private_address("not-an-ip"))
        self.assertFalse(is_private_address("::ffff:93.184.216.34"))

    def test_pinned_address_only_returns_previously_validated_addresses(self):
        addresses = [
            {"address": "93.184.216.34", "family": 4},
            {"address": "2606:2800:220:1:248:1893:25c8:1946", "family": 6},
        ]
        self.assertEqual(_pinned_address(addresses), addresses[0])
        with self.assertRaisesRegex(ValueError, "No allowed address"):
            _pinned_address([])

    def test_decode_entities_never_double_decodes(self):
        self.assertEqual(decode_entities("&amp;lt;"), "&lt;")
        self.assertEqual(decode_entities("&#233; &#xE9;"), "é é")
        self.assertEqual(decode_entities("&mdash; &unknown;"), "— &unknown;")

    def test_html_to_text_keeps_structure_readable(self):
        text = html_to_text(
            "<div><h2>Title</h2><p>Para<br>break</p><ul><li>a</li></ul></div>"
        )
        self.assertRegex(text, r"## Title")
        self.assertRegex(text, r"Para\nbreak")
        self.assertRegex(text, r"- a")

    def test_domain_matches_handles_subdomains_and_wildcards(self):
        self.assertTrue(domain_matches("example.com", "example.com"))
        self.assertTrue(domain_matches("example.com", "www.example.com"))
        self.assertTrue(domain_matches("*.example.com", "example.com"))
        self.assertFalse(domain_matches("example.com", "notexample.com"))

    def test_domain_matches_ignores_a_trailing_root_dot(self):
        self.assertTrue(domain_matches("example.com", "example.com."))
        self.assertTrue(domain_matches("example.com", "www.example.com."))
        self.assertTrue(domain_matches("example.com.", "example.com."))
        self.assertFalse(domain_matches("example.com", "notexample.com."))

    def test_a_trailing_root_dot_cannot_bypass_the_deny_list(self):
        with self.assertRaisesRegex(ValueError, "deny list"):
            tool(web_config({"allowPrivate": False, "denyDomains": ["example.com"]})).run(
                {"url": "http://example.com./x"}, CTX
            )
        with self.assertRaisesRegex(ValueError, "deny list"):
            tool(web_config({"allowPrivate": False, "denyDomains": ["example.com"]})).run(
                {"url": "http://www.example.com./x"}, CTX
            )


if __name__ == "__main__":
    unittest.main()
