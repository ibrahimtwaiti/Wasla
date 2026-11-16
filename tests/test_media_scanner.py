import hashlib
import os
import unittest
from unittest.mock import patch,MagicMock
from fastapi.testclient import TestClient
from scripts.media_scanner import app,scan_bytes

class Scanner(unittest.TestCase):
    def test_auth_checksum_clean_infected_and_outage(self):
        client=TestClient(app);raw=b'test bytes';headers={'Authorization':'Bearer '+'x'*32,'X-Content-SHA256':hashlib.sha256(raw).hexdigest()}
        with patch.dict(os.environ,{'WASLA_MEDIA_SCANNER_TOKEN':'x'*32}):
            self.assertEqual(client.post('/scan',content=raw).status_code,401)
            self.assertEqual(client.post('/scan',content=b'changed',headers=headers).status_code,422)
            for verdict in ['clean','infected']:
                with patch('scripts.media_scanner.scan_bytes',return_value=verdict):self.assertEqual(client.post('/scan',content=raw,headers=headers).json()['verdict'],verdict)
            with patch('scripts.media_scanner.scan_bytes',side_effect=OSError('offline')):self.assertEqual(client.post('/scan',content=raw,headers=headers).status_code,503)
            self.assertEqual(client.post('/scan',content=b'x'*(2*1024*1024+1),headers=headers).status_code,413)

    def test_clamd_binary_framing_and_no_clean_fallback(self):
        sock=MagicMock();sock.__enter__.return_value=sock
        with patch('scripts.media_scanner.socket.socket',return_value=sock):
            sock.recv.return_value=b'stream: OK\0';self.assertEqual(scan_bytes(b'abc'),'clean')
            self.assertEqual(sock.sendall.call_args_list[1].args[0],b'\0\0\0\x03abc')
            sock.recv.return_value=b'stream: size exceeded ERROR\0'
            with self.assertRaises(RuntimeError):scan_bytes(b'abc')
