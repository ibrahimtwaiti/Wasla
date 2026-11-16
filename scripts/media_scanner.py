"""Authenticated private ClamAV gateway. Run behind HTTPS; never expose clamd TCP."""
import hashlib
import hmac
import os
import socket
import struct
from fastapi import FastAPI, Request, HTTPException
from starlette.concurrency import run_in_threadpool

app=FastAPI(docs_url=None,redoc_url=None,openapi_url=None)
MAX_BYTES=2*1024*1024

def scan_bytes(raw):
    path=os.getenv('WASLA_CLAMD_SOCKET','/run/clamav/clamd.sock')
    with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as sock:
        sock.settimeout(15);sock.connect(path);sock.sendall(b'zINSTREAM\0')
        for offset in range(0,len(raw),65536):
            part=raw[offset:offset+65536];sock.sendall(struct.pack('!I',len(part))+part)
        sock.sendall(b'\0\0\0\0');reply=b''
        while b'\0' not in reply and len(reply)<4096:
            part=sock.recv(1024)
            if not part:break
            reply+=part
    response=reply.split(b'\0',1)[0]
    if response==b'stream: OK':return 'clean'
    if response.endswith(b' FOUND'):return 'infected'
    raise RuntimeError('scanner_no_definitive_verdict')

@app.post('/scan')
async def scan(request:Request):
    token=os.getenv('WASLA_MEDIA_SCANNER_TOKEN','')
    if len(token)<32:raise HTTPException(503,'scanner_secret_not_configured')
    if not hmac.compare_digest(request.headers.get('authorization','').encode(),('Bearer '+token).encode()):raise HTTPException(401,'unauthorized')
    chunks=[];size=0
    async for part in request.stream():
        size+=len(part)
        if size>MAX_BYTES:raise HTTPException(413,'size_limit')
        chunks.append(part)
    if not size:raise HTTPException(422,'empty_file')
    raw=b''.join(chunks);checksum=hashlib.sha256(raw).hexdigest()
    if not hmac.compare_digest(checksum,request.headers.get('x-content-sha256','')):raise HTTPException(422,'checksum_mismatch')
    try:verdict=await run_in_threadpool(scan_bytes,raw)
    except Exception:raise HTTPException(503,'scanner_unavailable')
    return {'verdict':verdict,'sha256':checksum}
