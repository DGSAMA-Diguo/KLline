from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer


class CorsHandler(SimpleHTTPRequestHandler):
    def end_headers(self):
        # 仅供已登录的浏览器读取本机更新分片。
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        super().end_headers()


ThreadingHTTPServer(("127.0.0.1", 18767), CorsHandler).serve_forever()
