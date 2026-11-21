"""Безопасность веб-дашборда scripts.ui и протокола среды (ревью безопасности 20.11 и ревью PR #22).

1. чужой сайт не читает API и не запускает генерацию (нет CORS, проверка Origin и Host);
2. /api/episode принимает только ID эпизода, а не произвольный путь;
3. данные агента экранируются в HTML, экспорт не ломает тег <script>;
4. API и экспорт не раскрывают абсолютные пути на машине автора;
5. неверный policy и сбой генерации дают ответ с ошибкой, а не обрыв соединения;
6. JSON с очень глубокой вложенностью даёт BAD_REQUEST, а не INTERNAL.
"""
import http.client
import http.server
import json
import threading
from urllib.parse import quote

import pytest

from scripts import ui
from env.client import AuditEnv

EP = "EP-0HJPAE81"
# Запас на нагруженную машину (полный прогон тестов на Windows): тесту нужен ответ, а не скорость
HTTP_TIMEOUT = 120


@pytest.fixture(scope="module")
def server():
    # Многопоточный сервер: медленное соединение одного теста не задерживает ответы следующим
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), ui.AuditUIRequestHandler)
    httpd.daemon_threads = True
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture
def no_generate(monkeypatch):
    """Генерация подменена: даже при регрессии защиты тест не пишет эпизод в episodes/ репозитория."""
    calls = []

    def fake(*args, **kwargs):
        calls.append(args)
        raise RuntimeError("generation is disabled in tests")

    monkeypatch.setattr(ui, "generate_episode", fake)
    return calls


def _req(port, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=HTTP_TIMEOUT)
    data = json.dumps(body).encode() if body is not None else None
    conn.request(method, path, body=data, headers=headers or {})
    resp = conn.getresponse()
    out = (resp.status, dict(resp.getheaders()), resp.read())
    conn.close()
    return out


# ---------------------------------------------------------------- Origin / Host

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
def test_foreign_origin_is_forbidden(server, no_generate, method, path, body):
    status, headers, _ = _req(server, method, path, body, {"Origin": "http://evil.example"})
    assert status == 403
    assert "Access-Control-Allow-Origin" not in headers
    assert no_generate == []


def test_dns_rebinding_host_is_forbidden(server):
    status, _, _ = _req(server, "GET", "/api/episodes", headers={"Host": f"evil.example:{server}"})
    assert status == 403


def test_hostname_parsing():
    assert ui._hostname("127.0.0.1:8080") == "127.0.0.1"
    assert ui._hostname("localhost") == "localhost"
    assert ui._hostname("[::1]:8080") == "::1"
    assert ui._hostname("evil.example:8080") == "evil.example"


@pytest.mark.parametrize("bound, host, ok", [
    ("127.0.0.1", "127.0.0.1:8080", True),
    ("127.0.0.1", "localhost:8080", True),
    ("127.0.0.1", "evil.example:8080", False),
    ("0.0.0.0", "localhost:8080", True),
    ("0.0.0.0", "evil.example:8080", False),   # DNS rebinding при --host 0.0.0.0
    ("0.0.0.0", "192.168.1.5:8080", False),
    ("192.168.1.5", "192.168.1.5:8080", True),
    ("192.168.1.5", "evil.example:8080", False),
])
def test_host_allowed(bound, host, ok):
    assert ui._host_allowed(bound, host) is ok


# ---------------------------------------------------------------- входные данные

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
    status, _, _ = _req(server, "GET", "/api/episode?id=" + quote(bad_id))
    assert status == 404


def test_run_rejects_path_instead_of_id(server):
    status, _, _ = _req(server, "POST", "/api/run", {"episode_id": "episodes/train/" + EP})
    assert status == 404


@pytest.mark.parametrize("policy", [None, 1, ["smart"], "rm -rf", ""])
def test_run_rejects_unknown_policy(server, policy):
    status, _, _ = _req(server, "POST", "/api/run", {"episode_id": EP, "policy": policy})
    assert status == 400


def test_post_body_size_is_limited(server):
    conn = http.client.HTTPConnection("127.0.0.1", server, timeout=HTTP_TIMEOUT)
    conn.putrequest("POST", "/api/generate")
    conn.putheader("Content-Length", str(ui.MAX_BODY + 1))
    conn.endheaders()
    resp = conn.getresponse()
    assert resp.status == 413
    conn.close()


def test_bad_seed_is_bad_request(server, no_generate):
    status, _, _ = _req(server, "POST", "/api/generate", {"seed": "abc"})
    assert status == 400
    assert no_generate == []


def test_generation_failure_returns_500(server, no_generate):
    status, _, _ = _req(server, "POST", "/api/generate", {"seed": 7})
    assert status == 500 and len(no_generate) == 1


# ---------------------------------------------------------------- HTML и экспорт

def test_template_escapes_agent_data():
    tpl = ui.HTML_TEMPLATE
    assert "function esc(v)" in tpl
    for raw in ("${f.explanation}", "${e}</span>", "${act.tool}", "<th>${k}</th>"):
        assert raw not in tpl


def _embedded_data(html):
    return html.split("window.__INITIAL_DATA__ = ", 1)[1].split(";</script>", 1)[0]


def test_export_does_not_break_script_tag(tmp_path, monkeypatch):
    payload = "<!--<script></script><img src=x onerror=alert(1)>"
    real = ui.get_available_episodes
    monkeypatch.setattr(ui, "get_available_episodes",
                        lambda: [dict(real()[0], episode_id=payload)])
    monkeypatch.setattr(ui, "get_episode_details", lambda _id: {"explanation": payload})
    out = tmp_path / "ui.html"
    ui.export_standalone_html(out, None)
    data = _embedded_data(out.read_text(encoding="utf-8"))
    assert "<" not in data  # ни "</script>", ни "<!--" внутри данных
    assert json.loads(data)["current_episode"]["explanation"] == payload


def test_api_and_export_have_no_absolute_paths(tmp_path):
    root = str(ui.ROOT)
    assert all("path" not in ep for ep in ui.get_available_episodes())
    out = tmp_path / "ui.html"
    ui.export_standalone_html(out, EP)
    data = json.dumps(json.loads(_embedded_data(out.read_text(encoding="utf-8"))), ensure_ascii=False)
    assert root not in data


def test_committed_export_has_no_absolute_paths():
    html = (ui.ROOT / "docs" / "audit_ui.html").read_text(encoding="utf-8")
    data = json.loads(_embedded_data(html))
    assert all("path" not in ep for ep in data["episodes"])
    text = json.dumps(data, ensure_ascii=False)
    assert "Users" not in text and "/home/" not in text


# ---------------------------------------------------------------- протокол среды

def test_server_deep_json_is_bad_request():
    with AuditEnv(run_id="deep", runs_dir=None) as env:
        env._proc.stdin.write(b"[" * 200_000 + b"]" * 200_000 + b"\n")
        env._proc.stdin.flush()
        reply = json.loads(env._proc.stdout.readline())
    assert reply["error"]["code"] == "BAD_REQUEST"
