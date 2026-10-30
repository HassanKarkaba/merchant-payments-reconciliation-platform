"""Real TCP tests for the simulator adapter's elapsed attempt budget and cleanup."""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from django.test import SimpleTestCase,override_settings
from operations import provider


class ProviderDeadlineTests(SimpleTestCase):
    def setUp(self):
        self.closed=threading.Event()
        closed=self.closed
        class Handler(BaseHTTPRequestHandler):
            protocol_version='HTTP/1.1'
            def log_message(self,*args):pass
            def do_GET(self):
                if self.path=='/v1/fast/':
                    body=json.dumps({'ok':True}).encode()
                    self.send_response(200)
                    self.send_header('Content-Type','application/json')
                    self.send_header('Content-Length',str(len(body)))
                    self.end_headers();self.wfile.write(body)
                    return
                self.send_response(200)
                self.send_header('Content-Type','application/json')
                self.send_header('Content-Length','100000')
                self.end_headers()
                end=time.monotonic()+7
                try:
                    self.wfile.write(b'{');self.wfile.flush()
                    # Each byte arrives well inside a per-read socket timeout.
                    # Only an elapsed attempt deadline can stop this at5s.
                    while time.monotonic()<end:
                        self.wfile.write(b' ');self.wfile.flush();time.sleep(.1)
                except (BrokenPipeError,ConnectionResetError,OSError):
                    closed.set()
                self.close_connection=True
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
        # localhost exercises the bounded real resolver subprocess as well.
        self.settings=override_settings(SIMULATOR_ORIGIN='http://localhost:'+str(self.server.server_port),SIMULATOR_API_KEY='isolated-synthetic-service-key-'+'a'*32,SIMULATOR_KEY_ID='local-v1')
        self.settings.enable()

    def tearDown(self):
        self.settings.disable()
        self.server.shutdown();self.server.server_close();self.thread.join(timeout=2)

    def test_healthy_response_uses_current_authenticated_transport(self):
        self.assertEqual(provider.request('GET','/v1/fast/'),{'ok':True})

    def test_dripping_body_is_stopped_at_total_deadline_and_socket_closes(self):
        started=time.monotonic()
        with self.assertRaises(provider.ProviderUncertain):
            provider.request('GET','/v1/drip/')
        elapsed=time.monotonic()-started
        self.assertGreaterEqual(elapsed,4.5)
        self.assertLess(elapsed,6.25)
        self.assertTrue(self.closed.wait(2),'The peer must observe closed transport rather than residual reading')
