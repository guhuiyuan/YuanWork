"""Ask the running YuanWork webview what its DOM looks like.

The app runs in a WebView2.  WebView2 accepts extra browser arguments
through the environment, so the app can be started with a remote
debugging port and then asked, over the DevTools protocol, what is
actually in the page.

Why: some layout bugs are invisible to the unit tests.  The tests
check the *text* of the CSS; what the browser does with it is a
different question.  TASK-081 is one of them - the wiki view had the
page tree in its own column, and the other three panels collapsed to
height 0 because the grid pushed the tree into a second row.  Only
the page itself could tell.

How:
    set WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS=--remote-debugging-port=9222
    start the app
    .venv/Scripts/python.exe scripts/bench/cdp_probe.py '"...js..."'

An expression can also be read from a file: pass @path/to/file.js
"""

import base64
import json
import os
import socket
import struct
import sys
import urllib.request


def find_target():
    with urllib.request.urlopen("http://127.0.0.1:9222/json", timeout=5) as r:
        targets = json.loads(r.read())
    for t in targets:
        if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
            return t["webSocketDebuggerUrl"], t.get("title"), t.get("url")
    return None, None, None


def connect(url):
    # ws://127.0.0.1:9222/devtools/page/<id>
    rest = url.split("://", 1)[1]
    hostport, path = rest.split("/", 1)
    host, port = hostport.split(":")
    sock = socket.create_connection((host, int(port)), timeout=15)
    key = base64.b64encode(os.urandom(16)).decode()
    req = (
        f"GET /{path} HTTP/1.1\r\n"
        f"Host: {hostport}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n\r\n"
    )
    sock.sendall(req.encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise RuntimeError("the debug port closed the handshake")
        buf += chunk
    if b"101" not in buf.split(b"\r\n", 1)[0]:
        raise RuntimeError("handshake refused: " + buf.split(b"\r\n", 1)[0].decode())
    return sock, buf.split(b"\r\n\r\n", 1)[1]


def send_text(sock, payload):
    data = payload.encode()
    mask = os.urandom(4)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
    header = bytearray([0x81])
    n = len(data)
    if n < 126:
        header.append(0x80 | n)
    elif n < 65536:
        header.append(0x80 | 126)
        header += struct.pack(">H", n)
    else:
        header.append(0x80 | 127)
        header += struct.pack(">Q", n)
    sock.sendall(bytes(header) + mask + masked)


def read_frames(sock, leftover=b""):
    """Yield (opcode, payload) for every frame that arrives."""
    buf = leftover
    while True:
        while len(buf) < 2:
            chunk = sock.recv(65536)
            if not chunk:
                return
            buf += chunk
        b0, b1 = buf[0], buf[1]
        opcode = b0 & 0x0F
        length = b1 & 0x7F
        offset = 2
        if length == 126:
            while len(buf) < offset + 2:
                buf += sock.recv(65536)
            length = struct.unpack(">H", buf[offset:offset + 2])[0]
            offset += 2
        elif length == 127:
            while len(buf) < offset + 8:
                buf += sock.recv(65536)
            length = struct.unpack(">Q", buf[offset:offset + 8])[0]
            offset += 8
        while len(buf) < offset + length:
            chunk = sock.recv(65536)
            if not chunk:
                return
            buf += chunk
        payload = buf[offset:offset + length]
        buf = buf[offset + length:]
        yield opcode, payload


def evaluate(sock, expression, msg_id=1):
    send_text(sock, json.dumps({
        "id": msg_id,
        "method": "Runtime.evaluate",
        "params": {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": True,
        },
    }))
    for opcode, payload in read_frames(sock):
        if opcode != 1:
            continue
        msg = json.loads(payload)
        if msg.get("id") == msg_id:
            result = msg.get("result", {})
            if "exceptionDetails" in result:
                return {"error": result["exceptionDetails"]}
            return result.get("result", {}).get("value")


def main():
    expressions = sys.argv[1:] or [
        "JSON.stringify({"
        " innerWidth: window.innerWidth, innerHeight: window.innerHeight,"
        " dpr: window.devicePixelRatio,"
        " appClass: document.querySelector('.app')?.className,"
        " appRect: (r=>r?{x:r.x,y:r.y,w:r.width,h:r.height}:null)(document.querySelector('.app')?.getBoundingClientRect()),"
        " sidebar: (r=>r?{x:r.x,y:r.y,w:r.width,h:r.height,text:(document.querySelector('.app__sidebar')?.innerText||'').slice(0,60)}:null)(document.querySelector('.app__sidebar')?.getBoundingClientRect()),"
        " main: (r=>r?{x:r.x,y:r.y,w:r.width,h:r.height}:null)(document.querySelector('.app__main')?.getBoundingClientRect()),"
        " tree: (r=>r?{x:r.x,y:r.y,w:r.width,h:r.height}:null)(document.querySelector('.app__wiki-tree')?.getBoundingClientRect()),"
        " right: (r=>r?{x:r.x,y:r.y,w:r.width,h:r.height}:null)(document.querySelector('.app__right')?.getBoundingClientRect()),"
        " kids: [...(document.querySelector('.app')?.children||[])].map(c=>c.className+':'+JSON.stringify(c.getBoundingClientRect()))"
        "})",
    ]
    expressions = [
        open(a[1:], encoding="utf-8").read() if a.startswith("@") else a
        for a in expressions
    ]
    url, title, page = find_target()
    print("target:", title, page)
    sock, leftover = connect(url)
    for i, expr in enumerate(expressions, start=1):
        value = evaluate(sock, expr, msg_id=i)
        print("---", expr[:60])
        print(value)
    sock.close()


if __name__ == "__main__":
    main()
