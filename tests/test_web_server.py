# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Henk Duisterwinkel
import http.client
import json
import os
import shutil
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from basicagentpy.config import load_config
from basicagentpy.web.server import start_web_server
from tests.fixtures.fake_server import (
    finish_chunk,
    start_fake_provider,
    text_chunk,
    tool_chunk,
)


TOKEN = "test-token"


class WebServerTests(unittest.TestCase):
    def setup_server(self, *, base_url="http://127.0.0.1:9/v1", config=None):
        config = config or {}
        directory = tempfile.mkdtemp(prefix="basicagentpy-web-")
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        config_path = os.path.join(directory, "config.json")
        raw = {
            "provider": {
                "baseUrl": base_url,
                "model": "fake-model",
                **(config.get("provider") or {}),
            },
            "tools": config.get("tools")
            or {"files": {"roots": [{"path": directory, "access": "rw"}]}},
            "skills": {"paths": [], **(config.get("skills") or {})},
            "memory": config.get("memory") or {"dir": os.path.join(directory, "data")},
        }
        if config.get("agent"):
            raw["agent"] = config["agent"]
        with open(config_path, "w", encoding="utf-8") as handle:
            json.dump(raw, handle)

        loaded = load_config(flags={"config": config_path}, cwd=directory)
        started = start_web_server(
            config=loaded,
            flags={"config": config_path},
            cwd=directory,
            version="test",
            token=TOKEN,
            port=0,
        )
        self.addCleanup(started.close)
        base = f"http://127.0.0.1:{started.port}"
        auth = {"Authorization": f"Bearer {TOKEN}"}

        def json_request(path, method="GET", body=None, headers=None):
            connection = http.client.HTTPConnection(
                "127.0.0.1", started.port, timeout=15
            )
            request_headers = dict(auth)
            request_headers.update(headers or {})
            payload = None
            if body is not None:
                payload = json.dumps(body).encode("utf-8")
                request_headers["Content-Type"] = "application/json"
            connection.request(method, path, body=payload, headers=request_headers)
            response = connection.getresponse()
            raw_body = response.read()
            response.close()
            connection.close()
            try:
                data = json.loads(raw_body)
            except ValueError:
                data = None
            return response.status, data

        def stream(session_id, message, on_event=None):
            connection = http.client.HTTPConnection(
                "127.0.0.1", started.port, timeout=30
            )
            payload = json.dumps(
                {"sessionId": session_id, "message": message}
            ).encode("utf-8")
            connection.request(
                "POST",
                "/api/chat",
                body=payload,
                headers={**auth, "Content-Type": "application/json"},
            )
            response = connection.getresponse()
            if response.status != 200:
                data = response.read()
                response.close()
                connection.close()
                try:
                    return {"status": response.status, "events": [], "data": json.loads(data)}
                except ValueError:
                    return {"status": response.status, "events": [], "data": None}
            events = []
            while True:
                line = response.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                event = json.loads(line)
                events.append(event)
                if on_event:
                    on_event(event)
            response.close()
            connection.close()
            return {"status": response.status, "events": events}

        return directory, config_path, started, base, json_request, stream

    def test_auth_host_and_origin_checks_protect_the_api(self):
        directory, config_path, started, base, json_request, stream = self.setup_server()

        connection = http.client.HTTPConnection("127.0.0.1", started.port)
        connection.request("GET", "/api/status")
        response = connection.getresponse()
        self.assertEqual(response.status, 401)
        response.read()
        connection.close()

        connection = http.client.HTTPConnection("127.0.0.1", started.port)
        connection.request(
            "GET", "/api/status", headers={"Authorization": "Bearer wrong"}
        )
        response = connection.getresponse()
        self.assertEqual(response.status, 401)
        response.read()
        connection.close()

        status, data = json_request("/api/status")
        self.assertEqual(status, 200)

        connection = http.client.HTTPConnection("127.0.0.1", started.port)
        connection.request(
            "GET",
            "/api/status",
            headers={"Host": "evil.example", "Authorization": f"Bearer {TOKEN}"},
        )
        response = connection.getresponse()
        self.assertEqual(response.status, 403)
        response.read()
        connection.close()

        status, data = json_request(
            "/api/status", headers={"Origin": "http://evil.example"}
        )
        self.assertEqual(status, 403)
        status, data = json_request("/api/status", headers={"Origin": base})
        self.assertEqual(status, 200)

        connection = http.client.HTTPConnection("127.0.0.1", started.port)
        connection.request("GET", "/")
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        response.read()
        connection.close()

    def test_status_and_static_files(self):
        directory, config_path, started, base, json_request, stream = self.setup_server()

        connection = http.client.HTTPConnection("127.0.0.1", started.port)
        connection.request("GET", "/")
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        self.assertIn("text/html", response.getheader("content-type"))
        body = response.read().decode("utf-8")
        self.assertIn("basicagentpy", body)
        connection.close()

        for path in (
            "/app.css",
            "/assets/css/tokens.css",
            "/assets/fonts/MarcellusSC-Regular.ttf",
        ):
            connection = http.client.HTTPConnection("127.0.0.1", started.port)
            connection.request("GET", path)
            response = connection.getresponse()
            self.assertEqual(response.status, 200, path)
            response.read()
            connection.close()

        status, data = json_request("/api/status")
        self.assertEqual(status, 200)
        self.assertEqual(data["model"], "fake-model")
        self.assertTrue(data["memoryEnabled"])
        self.assertEqual(data["contextChars"], 100000)
        self.assertIn("write_file", data["tools"])
        self.assertIn("fetch_url", data["tools"])
        self.assertTrue(any(skill["name"] == "basicagentpy" for skill in data["skills"]))

        connection = http.client.HTTPConnection("127.0.0.1", started.port)
        connection.request("GET", "/%2e%2e/%2e%2e/etc/passwd")
        response = connection.getresponse()
        self.assertEqual(response.status, 404)
        response.read()
        connection.close()

        status, data = json_request("/api/nope")
        self.assertEqual(status, 404)

        connection = http.client.HTTPConnection("127.0.0.1", started.port)
        connection.request("POST", "/")
        response = connection.getresponse()
        self.assertEqual(response.status, 405)
        response.read()
        connection.close()

    def test_chat_streams_events_persists_the_session_and_lists_it(self):
        provider = start_fake_provider(
            [[text_chunk("Hello from the fake."), finish_chunk("stop")]]
        )
        self.addCleanup(provider.close)
        directory, config_path, started, base, json_request, stream = self.setup_server(
            base_url=provider.base_url
        )

        status, session = json_request("/api/sessions", method="POST", body={})
        self.assertEqual(status, 200)
        self.assertRegex(session["id"], r"^\d{14}-[0-9a-f]{4}$")
        self.assertEqual(session["contextSize"], 0)

        result = stream(session["id"], "say hi")
        self.assertEqual(result["status"], 200)
        self.assertEqual(
            [event["type"] for event in result["events"]],
            ["iteration", "assistant_delta", "done"],
        )
        self.assertIsInstance(result["events"][0]["contextSize"], int)
        self.assertGreater(result["events"][0]["contextSize"], 0)
        self.assertEqual(result["events"][1]["text"], "Hello from the fake.")
        self.assertEqual(result["events"][2]["text"], "Hello from the fake.")

        session_file = os.path.join(directory, "data", "sessions", f"{session['id']}.jsonl")
        self.assertTrue(os.path.exists(session_file))
        with open(session_file, encoding="utf-8") as handle:
            lines = [json.loads(line) for line in handle.read().strip().split("\n")]
        self.assertEqual([entry["role"] for entry in lines], ["user", "assistant"])

        status, listing = json_request("/api/sessions")
        self.assertTrue(listing["memoryEnabled"])
        self.assertEqual(len(listing["sessions"]), 1)
        self.assertEqual(listing["sessions"][0]["id"], session["id"])
        self.assertEqual(listing["sessions"][0]["messages"], 2)
        self.assertEqual(listing["sessions"][0]["title"], "say hi")

        status, transcript = json_request(f"/api/sessions/{session['id']}")
        self.assertEqual(len(transcript["messages"]), 2)
        self.assertEqual(transcript["messages"][0]["role"], "user")
        self.assertTrue(transcript["messages"][0]["ts"])
        self.assertGreater(transcript["contextSize"], 0)

        status, data = json_request("/api/chat", method="POST", body={"message": "hi"})
        self.assertEqual(status, 400)
        status, data = json_request(
            "/api/chat",
            method="POST",
            body={"sessionId": session["id"], "message": "   "},
        )
        self.assertEqual(status, 400)

    def test_sessions_can_be_deleted_from_the_memory_api(self):
        provider = start_fake_provider([[text_chunk("Hi."), finish_chunk("stop")]])
        self.addCleanup(provider.close)
        directory, config_path, started, base, json_request, stream = self.setup_server(
            base_url=provider.base_url
        )

        status, first = json_request(
            "/api/sessions", method="POST", body={"id": "session-one"}
        )
        status2, second = json_request(
            "/api/sessions", method="POST", body={"id": "session-two"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(status2, 200)
        stream(first["id"], "remember this")
        status, listing = json_request("/api/sessions")
        self.assertEqual(len(listing["sessions"]), 2)

        status, deleted = json_request(f"/api/sessions/{first['id']}", method="DELETE")
        self.assertEqual(status, 200)
        self.assertTrue(deleted["removed"])
        self.assertEqual(deleted["lastSessionId"], second["id"])

        status, listing = json_request("/api/sessions")
        self.assertEqual([item["id"] for item in listing["sessions"]], [second["id"]])
        status, transcript = json_request(f"/api/sessions/{first['id']}")
        self.assertEqual(transcript["messages"], [])
        self.assertFalse(
            os.path.exists(os.path.join(directory, "data", "sessions", f"{first['id']}.jsonl"))
        )

        status, again = json_request(f"/api/sessions/{first['id']}", method="DELETE")
        self.assertEqual(status, 200)
        self.assertFalse(again["removed"])
        status, data = json_request("/api/sessions/", method="DELETE")
        self.assertEqual(status, 404)

    def test_tool_calls_are_executed_audited_and_streamed_over_the_api(self):
        provider = start_fake_provider(
            [
                [
                    tool_chunk(
                        index=0,
                        id="c1",
                        name="write_file",
                        args='{"path":"hello.txt","content":"hi from web"}',
                    ),
                    finish_chunk("tool_calls"),
                ],
                [text_chunk("Created hello.txt."), finish_chunk("stop")],
            ]
        )
        self.addCleanup(provider.close)
        directory, config_path, started, base, json_request, stream = self.setup_server(
            base_url=provider.base_url
        )

        status, session = json_request("/api/sessions", method="POST", body={})
        result = stream(session["id"], "create hello.txt")
        tool_end = next(event for event in result["events"] if event["type"] == "tool_end")
        self.assertEqual(tool_end["name"], "write_file")
        self.assertTrue(tool_end["ok"])
        self.assertIsInstance(tool_end["durationMs"], int)
        with open(os.path.join(directory, "hello.txt"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "hi from web")

        audit_path = os.path.join(directory, "data", "audit.jsonl")
        self.assertTrue(os.path.exists(audit_path))
        with open(audit_path, encoding="utf-8") as handle:
            entry = json.loads(handle.read().strip().split("\n")[0])
        self.assertEqual(entry["tool"], "write_file")
        self.assertTrue(entry["ok"])

        status, audit = json_request("/api/audit")
        self.assertEqual(status, 200)
        self.assertEqual(audit["total"], 1)
        self.assertEqual(audit["entries"][0]["tool"], "write_file")
        status, filtered = json_request("/api/audit?tool=write_file&ok=true")
        self.assertEqual(filtered["total"], 1)
        status, failed = json_request("/api/audit?ok=false")
        self.assertEqual(failed["total"], 0)

    def test_approval_pauses_the_turn_until_the_browser_approves(self):
        provider = start_fake_provider(
            [
                [
                    tool_chunk(
                        index=0,
                        id="c1",
                        name="write_file",
                        args='{"path":"protected.txt","content":"approved"}',
                    ),
                    finish_chunk("tool_calls"),
                ],
                [text_chunk("Written."), finish_chunk("stop")],
            ]
        )
        self.addCleanup(provider.close)
        directory, config_path, started, base, json_request, stream = self.setup_server(
            base_url=provider.base_url,
            config={
                "tools": {
                    "files": {
                        "roots": [{"path": ".", "access": "rw"}],
                        "protectedPaths": ["protected.txt"],
                    }
                }
            },
        )

        status, session = json_request("/api/sessions", method="POST", body={})
        approval_seen = threading.Event()
        seen = {}

        def on_event(event):
            if event["type"] == "approval":
                seen.update(event)
                approval_seen.set()

        stream_thread = threading.Thread(
            target=lambda: stream(session["id"], "write protected.txt", on_event),
            daemon=True,
        )
        stream_thread.start()
        self.assertTrue(approval_seen.wait(10))
        self.assertRegex(seen["description"], r"protected\.txt")
        self.assertIsInstance(seen["id"], str)

        status, data = json_request(
            f"/api/approvals/{seen['id']}", method="POST", body={"approved": True}
        )
        self.assertEqual(status, 200)
        status, data = json_request(
            f"/api/approvals/{seen['id']}", method="POST", body={"approved": True}
        )
        self.assertEqual(status, 404)
        stream_thread.join(timeout=10)
        with open(os.path.join(directory, "protected.txt"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "approved")

    def test_denied_approval_blocks_the_guarded_write(self):
        provider = start_fake_provider(
            [
                [
                    tool_chunk(
                        index=0,
                        id="c1",
                        name="write_file",
                        args='{"path":"protected.txt","content":"nope"}',
                    ),
                    finish_chunk("tool_calls"),
                ],
                [text_chunk("I did not write it."), finish_chunk("stop")],
            ]
        )
        self.addCleanup(provider.close)
        directory, config_path, started, base, json_request, stream = self.setup_server(
            base_url=provider.base_url,
            config={
                "tools": {
                    "files": {
                        "roots": [{"path": ".", "access": "rw"}],
                        "protectedPaths": ["protected.txt"],
                    }
                }
            },
        )

        status, session = json_request("/api/sessions", method="POST", body={})
        approval_seen = threading.Event()
        seen = {}

        def on_event(event):
            if event["type"] == "approval":
                seen.update(event)
                approval_seen.set()

        result_box = {}
        thread = threading.Thread(
            target=lambda: result_box.update(
                stream(session["id"], "write protected.txt", on_event)
            ),
            daemon=True,
        )
        thread.start()
        self.assertTrue(approval_seen.wait(10))
        status, data = json_request(
            f"/api/approvals/{seen['id']}", method="POST", body={"approved": False}
        )
        self.assertEqual(status, 200)
        thread.join(timeout=10)
        tool_end = next(
            event
            for event in result_box["events"]
            if event["type"] == "tool_end"
        )
        self.assertFalse(tool_end["ok"])
        self.assertFalse(os.path.exists(os.path.join(directory, "protected.txt")))
        status, data = json_request(
            "/api/approvals/does-not-exist", method="POST", body={"approved": True}
        )
        self.assertEqual(status, 404)

    def test_a_running_turn_reports_busy_and_can_be_aborted(self):
        provider = start_hanging_provider()
        self.addCleanup(provider.close)
        directory, config_path, started, base, json_request, stream = self.setup_server(
            base_url=provider.base_url
        )

        status, session = json_request("/api/sessions", method="POST", body={})
        iteration_seen = threading.Event()

        def on_event(event):
            if event["type"] == "iteration":
                iteration_seen.set()

        result_box = {}
        thread = threading.Thread(
            target=lambda: result_box.update(
                stream(session["id"], "hang", on_event)
            ),
            daemon=True,
        )
        thread.start()
        self.assertTrue(iteration_seen.wait(10))
        status, data = json_request(
            "/api/chat",
            method="POST",
            body={"sessionId": session["id"], "message": "again"},
        )
        self.assertEqual(status, 409)
        status, data = json_request(
            f"/api/sessions/{session['id']}", method="DELETE"
        )
        self.assertEqual(status, 409)
        status, data = json_request(
            "/api/chat/abort", method="POST", body={"sessionId": session["id"]}
        )
        self.assertEqual(status, 200)
        thread.join(timeout=10)
        self.assertTrue(
            any(event["type"] == "aborted" for event in result_box["events"])
        )
        status, deleted = json_request(
            f"/api/sessions/{session['id']}", method="DELETE"
        )
        self.assertEqual(status, 200)
        self.assertTrue(deleted["removed"])
        self.assertIsNone(deleted["lastSessionId"])

    def test_facts_can_be_listed_added_and_removed(self):
        directory, config_path, started, base, json_request, stream = self.setup_server()

        status, data = json_request("/api/facts")
        self.assertEqual(data["facts"], [])
        status, added = json_request(
            "/api/facts", method="PUT", body={"key": "editor", "value": "nvim"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(added["fact"]["key"], "editor")
        status, data = json_request(
            "/api/facts", method="PUT", body={"key": "editor"}
        )
        self.assertEqual(status, 400)
        status, listed = json_request("/api/facts")
        self.assertEqual(len(listed["facts"]), 1)
        self.assertEqual(listed["facts"][0]["value"], "nvim")
        self.assertTrue(os.path.exists(os.path.join(directory, "data", "facts.jsonl")))
        status, removed = json_request("/api/facts/editor", method="DELETE")
        self.assertTrue(removed["removed"])
        status, removed = json_request("/api/facts/editor", method="DELETE")
        self.assertFalse(removed["removed"])
        status, data = json_request("/api/facts")
        self.assertEqual(data["facts"], [])

    def test_config_can_be_read_validated_saved_and_reloaded_with_key_preservation(self):
        directory, config_path, started, base, json_request, stream = self.setup_server(
            config={"provider": {"apiKey": "sekret-key-1234567890"}}
        )
        data_dir = os.path.join(directory, "data")

        status, initial = json_request("/api/config")
        self.assertTrue(initial["found"])
        self.assertTrue(initial["apiKeySet"])
        self.assertEqual(initial["raw"]["provider"]["apiKey"], "")
        self.assertRegex(initial["effective"]["provider"]["apiKey"], r"^sek\.\.\.")
        self.assertNotIn("1234567890", initial["effective"]["provider"]["apiKey"])
        self.assertTrue(initial["path"].endswith("config.json"))

        status, invalid = json_request(
            "/api/config",
            method="PUT",
            body={"config": {"provider": {"temperature": 9}}},
        )
        self.assertEqual(status, 400)
        self.assertRegex(invalid["error"], "temperature")

        status, saved = json_request(
            "/api/config",
            method="PUT",
            body={
                "config": {
                    "provider": {"model": "new-model", "apiKey": ""},
                    "skills": {"paths": []},
                    "memory": {"dir": data_dir},
                }
            },
        )
        self.assertEqual(status, 200, saved)
        self.assertEqual(saved["effective"]["provider"]["model"], "new-model")

        with open(initial["path"], encoding="utf-8") as handle:
            on_disk = json.load(handle)
        self.assertEqual(on_disk["provider"]["model"], "new-model")
        self.assertEqual(on_disk["provider"]["apiKey"], "sekret-key-1234567890")
        self.assertEqual(os.stat(initial["path"]).st_mode & 0o777, 0o600)

        status, current = json_request("/api/status")
        self.assertEqual(current["model"], "new-model")

        status, cleared = json_request(
            "/api/config",
            method="PUT",
            body={
                "config": {
                    "provider": {"apiKey": None},
                    "skills": {"paths": []},
                    "memory": {"dir": data_dir},
                }
            },
        )
        self.assertEqual(status, 200)
        self.assertFalse(cleared["apiKeySet"])
        with open(initial["path"], encoding="utf-8") as handle:
            self.assertNotIn("apiKey", json.load(handle)["provider"])

    def test_memory_disabled_turns_off_facts_sessions_and_the_audit_api(self):
        provider = start_fake_provider([[text_chunk("No memory here."), finish_chunk("stop")]])
        self.addCleanup(provider.close)
        directory, config_path, started, base, json_request, stream = self.setup_server(
            base_url=provider.base_url, config={"memory": {"enabled": False}}
        )

        status, data = json_request("/api/status")
        self.assertFalse(data["memoryEnabled"])
        self.assertIsNone(data["dataDir"])
        status, data = json_request("/api/facts")
        self.assertEqual(status, 400)
        status, data = json_request("/api/audit")
        self.assertEqual(status, 400)
        status, data = json_request("/api/sessions/whatever", method="DELETE")
        self.assertEqual(status, 400)
        status, data = json_request("/api/sessions")
        self.assertEqual(data, {"memoryEnabled": False, "sessions": []})
        status, session = json_request("/api/sessions", method="POST", body={})
        self.assertEqual(session["messages"], [])
        result = stream(session["id"], "hello")
        self.assertTrue(any(event["type"] == "done" for event in result["events"]))


def start_hanging_provider():
    stop = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def log_message(self, format, *args):
            return

        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.end_headers()
            try:
                self.wfile.write(
                    b'data: {"choices":[{"index":0,"delta":{"content":"thinking "},"finish_reason":null}]}\n\n'
                )
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return
            stop.wait(30)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    server.block_on_close = False
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    class HangingProvider:
        base_url = f"http://127.0.0.1:{server.server_address[1]}/v1"

        def close(self):
            stop.set()
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    return HangingProvider()


if __name__ == "__main__":
    unittest.main()
