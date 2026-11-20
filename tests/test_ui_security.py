"""Безопасность веб-дашборда scripts.ui и протокола среды (находки ревью безопасности 20.11).

1. чужой сайт не читает API и не запускает генерацию (нет CORS, проверка Origin и Host);
2. /api/episode принимает только ID эпизода, а не произвольный путь;
3. данные агента экранируются в HTML, экспорт не закрывает тег <script>;
4. JSON с очень глубокой вложенностью даёт BAD_REQUEST, а не INTERNAL.
"""
import http.client
import http.server
import json
import threading

import pytest

from scripts import ui
from env.client import AuditEnv

EP = "EP-0HJPAE81"


@pytest.fixture(scope="module")
def server():
    httpd = http.server.HTTPServer(("127.0.0.1", 0), ui.AuditUIRequestHandler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


def _req(port, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    data = json.dumps(body).encode() if body is not None else None
    conn.request(method, path, body=data, headers=headers or {})
    resp = conn.getresponse()
    out = (resp.status, dict(resp.getheaders()), resp.read())
    conn.close()
    return out


def test_own_page_reads_episode_without_cors_header(server):
    origin = {"Origin": f"http://127.0.0.1:{server}"}
    status, headers, body = _req(server, "GET", f"/api/episode?id={EP}", headers=origin)
    assert status == 200 and json.loads(body)["episode_id"] == EP
    assert "Access-Control-Allow-Origin" not in headers


@pytest.mark.parametrize("method, path, body", [
    ("GET", "/api/episodes", None),
    ("GET", f"/api/episode?id={EP}", None),
    ("POST", "/api/run", {"episode_id": EP, "policy": "naive"}),
    ("POST", "/api/generate", {"seed": 7}),
])
def test_foreign_origin_is_forbidden(server, method, path, body):
    status, headers, _ = _req(server, method, path, body, {"Origin": "http://evil.example"})
    assert status == 403
    assert "Access-Control-Allow-Origin" not in headers


def test_dns_rebinding_host_is_forbidden(server):
    status, _, _ = _req(server, "GET", "/api/episodes", headers={"Host": f"evil.example:{server}"})
    assert status == 403


@pytest.mark.parametrize("bad_id", [
    "episodes/train/EP-0HJPAE81",
    "..",
    ".",
    "*",
    "EP-0HJPAE8*",
    "C:/Windows",
    "ep-0hjpae81",
])
def test_episode_endpoint_accepts_only_ids(server, bad_id):
    from urllib.parse import quote
    status, _, _ = _req(server, "GET", "/api/episode?id=" + quote(bad_id))
    assert status == 404


def test_run_rejects_path_instead_of_id(server):
    status, _, _ = _req(server, "POST", "/api/run", {"episode_id": "episodes/train/" + EP})
    assert status == 404


def test_post_body_size_is_limited(server):
    conn = http.client.HTTPConnection("127.0.0.1", server, timeout=30)
    conn.putrequest("POST", "/api/generate")
    conn.putheader("Content-Length", str(ui.MAX_BODY + 1))
    conn.endheaders()
    resp = conn.getresponse()
    assert resp.status == 413
    conn.close()


def test_bad_seed_is_bad_request(server):
    status, _, _ = _req(server, "POST", "/api/generate", {"seed": "abc"})
    assert status == 400


def test_hostname_parsing():
    assert ui._hostname("127.0.0.1:8080") == "127.0.0.1"
    assert ui._hostname("localhost") == "localhost"
    assert ui._hostname("[::1]:8080") == "::1"
    assert ui._hostname("evil.example:8080") == "evil.example"


def test_template_escapes_agent_data():
    tpl = ui.HTML_TEMPLATE
    assert "function esc(v)" in tpl
    for raw in ("${f.explanation}", "${e}</span>", "${act.tool}", "<th>${k}</th>"):
        assert raw not in tpl


def test_export_does_not_close_script_tag(tmp_path, monkeypatch):
    payload = "</script><img src=x onerror=alert(1)>"
    real = ui.get_available_episodes
    monkeypatch.setattr(ui, "get_available_episodes",
                        lambda: [dict(real()[0], episode_id=payload)])
    monkeypatch.setattr(ui, "get_episode_details", lambda _id: {"explanation": payload})
    out = tmp_path / "ui.html"
    ui.export_standalone_html(out, None)
    html = out.read_text(encoding="utf-8")
    assert payload not in html
    assert "<\\/script><img" in html


def test_server_deep_json_is_bad_request():
    with AuditEnv(run_id="deep", runs_dir=None) as env:
        env._proc.stdin.write(b"[" * 200_000 + b"]" * 200_000 + b"\n")
        env._proc.stdin.flush()
        reply = json.loads(env._proc.stdout.readline())
    assert reply["error"]["code"] == "BAD_REQUEST"
