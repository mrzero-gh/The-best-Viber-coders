"""Веб-интерфейс и локальный дашборд для симулятора Audit Gym (UI).

Красивый минималистичный интерфейс для демонстрации жюри и интерактивного исследования:
1. Пошаговый плеер траектории агента (play/pause, слайдер, просмотр каждого шага и наблюдений);
2. Карточки выявленных нарушений и доказательств с проверкой эталоном (TP / FP / FN);
3. Сравнительный бенчмарк 4 базовых политик (Oracle, Smart Auditor, Naive Auditor, Flag All);
4. Просмотр открытых бухгалтерских книг (Invoices, Payments, Ledger Дт/Кт, FX);
5. Интерактивная генерация и запуск эпизодов по любому случайному Seed.

Запуск:
    python -m scripts.ui                     # Запуск локального веб-сервера на порту 8080
    python -m scripts.ui --open              # Запуск с авто-открытием в браузере
    python -m scripts.ui --export docs/ui.html # Экспорт автономного HTML-отчёта (без сервера)
"""
import argparse
import copy
import http.server
import json
import os
import re
import socket
import sys
import tempfile
import threading
import traceback
import urllib.parse
import webbrowser
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent

from common.jsonio import canonical_dumps, read_jsonl
from env.client import AuditEnv
from generator.build import generate_episode
from grader.evaluate import evaluate
from grader.matcher import match
from policies import FlagAllPolicy, OraclePolicy, RandomPolicy, RuleBasedPolicy
from scripts.collect_episodes import DEV_CONFIG, TRAIN_CONFIG


def format_money(minor: int, currency: str, exp: int = 2) -> str:
    sign = "−" if minor < 0 else ""
    abs_minor = abs(minor)
    if exp == 0:
        return f"{sign}{abs_minor:,}".replace(",", " ") + f" {currency}"
    whole, frac = divmod(abs_minor, 10 ** exp)
    text = f"{whole:,}".replace(",", " ")
    return f"{sign}{text},{frac:0{exp}d} {currency}"


def get_available_episodes() -> List[Dict[str, Any]]:
    """Собирает список доступных train и dev эпизодов с краткой статистикой."""
    res = []
    base_dirs = [
        ("train", ROOT / "episodes" / "train"),
        ("dev", ROOT / "episodes" / "dev"),
    ]

    for mode, bdir in base_dirs:
        if not bdir.exists():
            continue
        for ep_dir in sorted(bdir.iterdir()):
            if not ep_dir.is_dir() or ep_dir.name.startswith("."):
                continue
            manifest_file = ep_dir / "public" / "manifest.json"
            if not manifest_file.exists():
                continue
            try:
                manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
            except Exception:
                continue

            traj_file = ep_dir / "trajectories.jsonl"
            steps_count = 0
            findings_count = 0
            reward = 0.0
            if traj_file.exists():
                lines = [line.strip() for line in traj_file.read_text(encoding="utf-8").splitlines() if line.strip()]
                steps_count = len(lines)
                if lines:
                    try:
                        last_step = json.loads(lines[-1])
                        reward = float(last_step.get("reward", 0.0))
                        act = last_step.get("action") or {}
                        findings_count = len(((act.get("args") or {}).get("findings") or {}).get("findings", []))
                    except Exception:
                        pass

            res.append({
                "episode_id": ep_dir.name,
                "mode": mode,
                "base_currency": manifest.get("base_currency", "KZT"),
                "timezone": manifest.get("timezone", "+05:00"),
                "period_start": manifest.get("period_start", "")[:10],
                "period_end": manifest.get("period_end", "")[:10],
                "counts": manifest.get("counts", {}),
                "steps_count": steps_count,
                "findings_count": findings_count,
                "reward": reward,
            })
    return res


def get_episode_details(ep_dir_or_id: str) -> Optional[Dict[str, Any]]:
    """Загружает полные данные эпизода (манифест, открытые таблицы, эталон, траекторию)."""
    p = Path(ep_dir_or_id)
    if not p.is_dir():
        # Поиск по ID
        found = None
        for candidate in [
            ROOT / "episodes" / "train" / ep_dir_or_id,
            ROOT / "episodes" / "dev" / ep_dir_or_id,
            ROOT / "episodes" / ep_dir_or_id,
        ]:
            if candidate.is_dir():
                found = candidate
                break
        if not found:
            matches = list((ROOT / "episodes").glob(f"**/{ep_dir_or_id}"))
            if matches:
                found = matches[0]
        if not found:
            return None
        p = found

    pub_dir = p / "public"
    priv_dir = p / "private"
    manifest_f = pub_dir / "manifest.json"
    if not manifest_f.exists():
        return None

    manifest = json.loads(manifest_f.read_text(encoding="utf-8"))
    exponents = manifest.get("currency_exponents", {})

    # Открытые таблицы
    invoices = []
    if (pub_dir / "invoices.jsonl").exists():
        invoices = read_jsonl(pub_dir / "invoices.jsonl")

    payments = []
    if (pub_dir / "payments.jsonl").exists():
        payments = read_jsonl(pub_dir / "payments.jsonl")

    ledger_rows = []
    if (pub_dir / "ledger.csv").exists():
        import csv
        with open(pub_dir / "ledger.csv", "r", encoding="utf-8") as f:
            ledger_rows = list(csv.DictReader(f))

    fx_rates = []
    if (pub_dir / "fx.csv").exists():
        import csv
        with open(pub_dir / "fx.csv", "r", encoding="utf-8") as f:
            fx_rates = list(csv.DictReader(f))

    # Приватный эталон
    truth = None
    if (priv_dir / "truth.json").exists():
        truth = json.loads((priv_dir / "truth.json").read_text(encoding="utf-8"))

    # Траектория
    trajectory = []
    traj_f = p / "trajectories.jsonl"
    if traj_f.exists():
        for line in traj_f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    trajectory.append(json.loads(line))
                except Exception:
                    continue

    # Извлечение submitted findings
    submitted_findings = []
    if trajectory:
        last = trajectory[-1]
        act = last.get("action") or {}
        if act.get("tool") == "submit":
            submitted_findings = ((act.get("args") or {}).get("findings") or {}).get("findings", [])

    # Сопоставление с эталоном
    matcher_res = None
    if truth and "issues" in truth and submitted_findings:
        matcher_res = match(submitted_findings, truth["issues"])

    return {
        "episode_id": p.name,
        "manifest": manifest,
        "invoices": invoices,
        "payments": payments,
        "ledger": ledger_rows,
        "fx": fx_rates,
        "truth": truth,
        "trajectory": trajectory,
        "submitted_findings": submitted_findings,
        "matcher_result": matcher_res,
    }


def run_policy_on_episode(ep_dir: Path, policy_name: str) -> Dict[str, Any]:
    """Запускает выбранную политику на эпизоде и возвращает траекторию с метриками."""
    truth = None
    priv_truth = ep_dir / "private" / "truth.json"
    if priv_truth.exists():
        truth = json.loads(priv_truth.read_text(encoding="utf-8"))

    policy_map = {
        "oracle": OraclePolicy(truth=truth),
        "smart": RuleBasedPolicy(naive=False),
        "naive": RuleBasedPolicy(naive=True),
        "flag_all": FlagAllPolicy(),
        "random": RandomPolicy(seed=42),
    }
    policy = policy_map.get(policy_name.lower(), RuleBasedPolicy(naive=False))
    policy.reset()

    pub_f = ep_dir / "public" / "manifest.json"
    manifest = json.loads(pub_f.read_text(encoding="utf-8"))
    ep_id = manifest["episode_id"]
    cfg = dict(TRAIN_CONFIG, mode="train")

    trajectory = []
    with AuditEnv(episode=str(ep_dir)) as env:
        obs, info = env.reset(seed=0, config=cfg)
        step_idx = 0
        trajectory.append({
            "episode_id": ep_id,
            "step": step_idx,
            "action": None,
            "observation": obs,
            "reward": 0.0,
            "terminated": False,
            "truncated": False,
        })

        terminated = truncated = False
        while not (terminated or truncated) and step_idx < 100:
            action = policy.act(obs)
            obs, reward, terminated, truncated, info = env.step(action)
            step_idx += 1
            trajectory.append({
                "episode_id": ep_id,
                "step": step_idx,
                "action": action,
                "observation": obs,
                "reward": float(reward),
                "terminated": bool(terminated),
                "truncated": bool(truncated),
            })

    # Извлекаем submitted findings и оцениваем
    submitted_findings = []
    last = trajectory[-1]
    act = last.get("action") or {}
    if act.get("tool") == "submit":
        submitted_findings = ((act.get("args") or {}).get("findings") or {}).get("findings", [])

    truth = None
    priv_truth = ep_dir / "private" / "truth.json"
    if priv_truth.exists():
        truth = json.loads(priv_truth.read_text(encoding="utf-8"))

    eval_res = None
    if truth and "issues" in truth:
        inspection_steps = max(0, len(trajectory) - 2)
        eval_res = evaluate({"episode_id": ep_id, "findings": submitted_findings}, truth, ep_id, inspection_steps)

    matcher_res = None
    if truth and "issues" in truth:
        matcher_res = match(submitted_findings, truth["issues"])

    return {
        "episode_id": ep_id,
        "policy_name": policy_name,
        "trajectory": trajectory,
        "submitted_findings": submitted_findings,
        "eval_result": eval_res,
        "matcher_result": matcher_res,
    }


def find_free_port(start_port: int = 8080) -> int:
    for port in range(start_port, start_port + 100):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("127.0.0.1", port))
                return port
        except OSError:
            continue
    return start_port


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Audit Gym // RL FinTech Simulator</title>
  <style>
    :root {
      --bg: #090d16;
      --surface: #111827;
      --surface-card: #1e293b;
      --border: #334155;
      --text: #f8fafc;
      --text-muted: #94a3b8;
      --primary: #10b981;
      --primary-glow: rgba(16, 185, 129, 0.2);
      --blue: #3b82f6;
      --amber: #f59e0b;
      --rose: #ef4444;
      --purple: #8b5cf6;
      --radius: 8px;
      --font-mono: ui-monospace, 'Cascadia Code', 'Fira Code', Menlo, Consolas, monospace;
      --font-sans: system-ui, -apple-system, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      background-color: var(--bg);
      color: var(--text);
      font-family: var(--font-sans);
      font-size: 14px;
      line-height: 1.5;
      min-height: 100vh;
      display: flex;
      flex-direction: column;
    }
    header {
      background-color: var(--surface);
      border-bottom: 1px solid var(--border);
      padding: 12px 24px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      flex-wrap: wrap;
    }
    .brand {
      display: flex;
      align-items: center;
      gap: 12px;
    }
    .logo-badge {
      background: linear-gradient(135deg, var(--primary), var(--blue));
      color: #000;
      font-weight: 800;
      font-size: 12px;
      padding: 4px 8px;
      border-radius: 4px;
      letter-spacing: 0.5px;
    }
    .brand-title {
      font-size: 16px;
      font-weight: 700;
      letter-spacing: -0.3px;
    }
    .brand-subtitle {
      font-size: 11px;
      color: var(--text-muted);
      font-family: var(--font-mono);
    }
    .header-controls {
      display: flex;
      align-items: center;
      gap: 10px;
      flex-wrap: wrap;
    }
    select, input, button {
      background-color: var(--surface-card);
      border: 1px solid var(--border);
      color: var(--text);
      padding: 6px 12px;
      border-radius: var(--radius);
      font-size: 13px;
      font-family: inherit;
      outline: none;
      transition: all 0.2s;
    }
    select:focus, input:focus { border-color: var(--primary); }
    button {
      cursor: pointer;
      font-weight: 600;
      display: inline-flex;
      align-items: center;
      gap: 6px;
    }
    button:hover { background-color: var(--border); }
    button.btn-primary {
      background-color: var(--primary);
      border-color: var(--primary);
      color: #042f2e;
    }
    button.btn-primary:hover {
      background-color: #059669;
      box-shadow: 0 0 12px var(--primary-glow);
    }
    main {
      flex: 1;
      padding: 20px 24px;
      max-width: 1440px;
      margin: 0 auto;
      width: 100%;
    }
    .metrics-ribbon {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      gap: 14px;
      margin-bottom: 20px;
    }
    .metric-card {
      background-color: var(--surface);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      padding: 14px 16px;
      display: flex;
      flex-direction: column;
      gap: 4px;
      position: relative;
      overflow: hidden;
    }
    .metric-card::before {
      content: '';
      position: absolute;
      top: 0; left: 0; width: 4px; height: 100%;
      background-color: var(--border);
    }
    .metric-card.primary::before { background-color: var(--primary); }
    .metric-card.blue::before { background-color: var(--blue); }
    .metric-card.amber::before { background-color: var(--amber); }
    .metric-card.purple::before { background-color: var(--purple); }
    .metric-title {
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 0.8px;
      color: var(--text-muted);
      font-weight: 600;
    }
    .metric-value {
      font-size: 24px;
      font-weight: 800;
      font-family: var(--font-mono);
      display: flex;
      align-items: baseline;
      gap: 6px;
    }
    .metric-sub {
      font-size: 11px;
      color: var(--text-muted);
    }
    .tabs-nav {
      display: flex;
      gap: 4px;
      border-bottom: 1px solid var(--border);
      margin-bottom: 20px;
    }
    .tab-btn {
      background: none;
      border: none;
      border-bottom: 2px solid transparent;
      border-radius: 0;
      color: var(--text-muted);
      padding: 10px 16px;
      font-size: 13px;
      font-weight: 600;
    }
    .tab-btn:hover { color: var(--text); background: none; }
    .tab-btn.active {
      color: var(--primary);
      border-bottom-color: var(--primary);
      background: none;
    }
    .tab-pane { display: none; }
    .tab-pane.active { display: block; }
    .player-box {
      background-color: var(--surface);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      padding: 16px;
      margin-bottom: 20px;
      display: flex;
      flex-direction: column;
      gap: 12px;
    }
    .player-controls {
      display: flex;
      align-items: center;
      gap: 12px;
      flex-wrap: wrap;
    }
    .scrubber-slider {
      flex: 1;
      height: 6px;
      accent-color: var(--primary);
      cursor: pointer;
    }
    .step-counter {
      font-family: var(--font-mono);
      font-size: 12px;
      color: var(--text-muted);
      min-width: 110px;
      text-align: right;
    }
    .step-display {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 16px;
    }
    @media (max-width: 900px) {
      .step-display { grid-template-columns: 1fr; }
    }
    .panel-card {
      background-color: var(--surface);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      padding: 16px;
      display: flex;
      flex-direction: column;
      gap: 12px;
    }
    .panel-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding-bottom: 8px;
      border-bottom: 1px solid var(--border);
    }
    .panel-title {
      font-size: 13px;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.5px;
      color: var(--text-muted);
    }
    .tag {
      display: inline-block;
      padding: 2px 8px;
      border-radius: 4px;
      font-size: 11px;
      font-weight: 700;
      font-family: var(--font-mono);
    }
    .tag-green { background: rgba(16, 185, 129, 0.15); color: #34d399; border: 1px solid rgba(16, 185, 129, 0.3); }
    .tag-blue { background: rgba(59, 130, 246, 0.15); color: #60a5fa; border: 1px solid rgba(59, 130, 246, 0.3); }
    .tag-amber { background: rgba(245, 158, 11, 0.15); color: #fbbf24; border: 1px solid rgba(245, 158, 11, 0.3); }
    .tag-rose { background: rgba(239, 68, 68, 0.15); color: #f87171; border: 1px solid rgba(239, 68, 68, 0.3); }
    .tag-purple { background: rgba(139, 92, 246, 0.15); color: #a78bfa; border: 1px solid rgba(139, 92, 246, 0.3); }
    pre, code {
      font-family: var(--font-mono);
      font-size: 12px;
    }
    pre {
      background-color: #060911;
      padding: 12px;
      border-radius: 6px;
      overflow-x: auto;
      border: 1px solid rgba(255, 255, 255, 0.05);
      white-space: pre-wrap;
    }
    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 12px;
    }
    th, td {
      padding: 8px 10px;
      text-align: left;
      border-bottom: 1px solid var(--border);
    }
    th {
      color: var(--text-muted);
      font-weight: 600;
      background-color: rgba(255, 255, 255, 0.02);
    }
    td.mono, th.mono { font-family: var(--font-mono); }
    tr:hover td { background-color: rgba(255, 255, 255, 0.03); }
    .findings-grid {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(360px, 1fr));
      gap: 16px;
    }
    .finding-card {
      background-color: var(--surface);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      padding: 16px;
      display: flex;
      flex-direction: column;
      gap: 8px;
    }
    .finding-card.tp { border-left: 4px solid var(--primary); }
    .finding-card.fp { border-left: 4px solid var(--rose); }
    .finding-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 8px;
    }
    .finding-type { font-weight: 700; font-size: 13px; }
    .finding-amount {
      font-size: 16px;
      font-weight: 800;
      font-family: var(--font-mono);
      color: var(--primary);
    }
    .finding-meta {
      font-size: 11px;
      color: var(--text-muted);
    }
    .pills-container {
      display: flex;
      flex-wrap: wrap;
      gap: 4px;
      margin-top: 4px;
    }
    .pill {
      font-family: var(--font-mono);
      font-size: 10px;
      background: var(--surface-card);
      border: 1px solid var(--border);
      padding: 2px 6px;
      border-radius: 4px;
      color: var(--text);
    }
    footer {
      border-top: 1px solid var(--border);
      padding: 16px 24px;
      font-size: 12px;
      color: var(--text-muted);
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-top: auto;
    }
  </style>
</head>
<body>

  <header>
    <div class="brand">
      <span class="logo-badge">AUDIT GYM</span>
      <div>
        <div class="brand-title">RL-среда финансового аудитора</div>
        <div class="brand-subtitle">Genesis FinTech 2026 // v1.0</div>
      </div>
    </div>

    <div class="header-controls">
      <label style="font-size: 12px; color: var(--text-muted)">Эпизод:</label>
      <select id="episodeSelect" onchange="onEpisodeChange(this.value)"></select>

      <label style="font-size: 12px; color: var(--text-muted)">Политика:</label>
      <select id="policySelect">
        <option value="smart">Smart Auditor (Rule-based)</option>
        <option value="oracle">Oracle Policy (Верхняя граница)</option>
        <option value="naive">Naive Auditor (Простая сверка)</option>
        <option value="flag_all">Flag All (Стресс-тест / Атака)</option>
        <option value="random">Random Policy (Случайный агент)</option>
      </select>

      <button class="btn-primary" onclick="runSelectedPolicy()">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg>
        Запустить аудит
      </button>

      <button onclick="generateRandomSeed()">
        🎲 Случайный seed
      </button>
    </div>
  </header>

  <main>
    <!-- Риббон основных метрик -->
    <div class="metrics-ribbon">
      <div class="metric-card primary">
        <div class="metric-title">Качество аудита (F1-мера)</div>
        <div class="metric-value" id="metricF1">1.000</div>
        <div class="metric-sub" id="metricPR">Точность (P): 100% | Полнота (R): 100%</div>
      </div>
      <div class="metric-card blue">
        <div class="metric-title">Терминальная награда</div>
        <div class="metric-value" id="metricReward">+0.9540</div>
        <div class="metric-sub" id="metricFormula">clip(2*F1 - 1 - 0.002*steps)</div>
      </div>
      <div class="metric-card amber">
        <div class="metric-title">Бюджет шагов</div>
        <div class="metric-value" id="metricSteps">24 <span style="font-size: 13px; font-weight: normal; color: var(--text-muted)">/ 100</span></div>
        <div class="metric-sub" id="metricStepsLeft">Осталось в запасе: 76 шагов</div>
      </div>
      <div class="metric-card purple">
        <div class="metric-title">Отвлекающие ловушки</div>
        <div class="metric-value" id="metricDistractors">4 / 4</div>
        <div class="metric-sub">Успешно обойдены без FP</div>
      </div>
    </div>

    <!-- Навигация по вкладкам -->
    <div class="tabs-nav">
      <button class="tab-btn active" onclick="switchTab('trajectory')">🎬 Плеер траектории (RL Loop)</button>
      <button class="tab-btn" onclick="switchTab('findings')">📋 Выявленные нарушения (Findings)</button>
      <button class="tab-btn" onclick="switchTab('benchmark')">⚖️ Эталон и политики (Benchmark)</button>
      <button class="tab-btn" onclick="switchTab('data')">📁 Открытые книги (Ledger & Docs)</button>
    </div>

    <!-- Вкладка 1: Плеер траектории -->
    <div id="tab-trajectory" class="tab-pane active">
      <div class="player-box">
        <div class="player-controls">
          <button onclick="playerFirst()">⏮</button>
          <button onclick="playerPrev()">◀</button>
          <button id="playBtn" class="btn-primary" onclick="togglePlay()">▶ Плей</button>
          <button onclick="playerNext()">▶</button>
          <button onclick="playerLast()">⏭</button>

          <input type="range" id="stepSlider" class="scrubber-slider" min="0" max="0" value="0" oninput="onSliderSeek(this.value)">

          <div class="step-counter" id="stepCounterLabel">Шаг 0 / 0</div>

          <select id="playSpeed" style="padding: 4px 8px; font-size: 11px;">
            <option value="1000">1x (1 с)</option>
            <option value="500" selected>2x (0.5 с)</option>
            <option value="200">5x (0.2 с)</option>
            <option value="50">20x (быстро)</option>
          </select>
        </div>
      </div>

      <div class="step-display">
        <!-- Левая колонка: Действие -->
        <div class="panel-card">
          <div class="panel-header">
            <span class="panel-title">Действие агента (Action)</span>
            <span id="stepToolBadge" class="tag tag-blue">RESET / INITIAL</span>
          </div>
          <div id="stepActionContent"></div>
        </div>

        <!-- Правая колонка: Наблюдение -->
        <div class="panel-card">
          <div class="panel-header">
            <span class="panel-title">Наблюдение среды (Observation)</span>
            <span id="stepObsKindBadge" class="tag tag-green">INITIAL OBS</span>
          </div>
          <div id="stepObservationContent"></div>
        </div>
      </div>
    </div>

    <!-- Вкладка 2: Находки -->
    <div id="tab-findings" class="tab-pane">
      <div style="margin-bottom: 14px; display: flex; justify-content: space-between; align-items: center;">
        <div style="font-size: 13px; color: var(--text-muted)">
          Официальные выводы (<code class="mono">findings.json</code>), отправленные агентом в действии <code class="mono">submit</code>:
        </div>
        <div id="findingsTotalBadge" class="tag tag-green">0 находок</div>
      </div>
      <div class="findings-grid" id="findingsGrid"></div>
    </div>

    <!-- Вкладка 3: Эталон и политики -->
    <div id="tab-benchmark" class="tab-pane">
      <div class="panel-card" style="margin-bottom: 20px;">
        <div class="panel-header">
          <span class="panel-title">Сравнение 4 базовых политик на текущем эпизоде</span>
          <span class="tag tag-purple">РЕГЛАМЕНТ 06</span>
        </div>
        <table>
          <thead>
            <tr>
              <th>Политика</th>
              <th>Роль в регламенте</th>
              <th>Шаги</th>
              <th>Точность (P)</th>
              <th>Полнота (R)</th>
              <th>F1-мера</th>
              <th>Терминальная награда</th>
              <th>Вердикт</th>
            </tr>
          </thead>
          <tbody id="policiesBenchmarkTable">
            <tr>
              <td><strong>Oracle Policy</strong></td>
              <td>Верхняя граница разрешимости (Gold standard)</td>
              <td class="mono">0</td>
              <td class="mono">100.0%</td>
              <td class="mono">100.0%</td>
              <td class="mono" style="color: var(--primary)">1.000</td>
              <td class="mono" style="color: var(--primary); font-weight: bold;">+1.0000</td>
              <td><span class="tag tag-green">МАКСИМУМ</span></td>
            </tr>
            <tr>
              <td><strong>Smart Auditor (RuleBased)</strong></td>
              <td>Автономный эвристический агент (A/D)</td>
              <td class="mono">24</td>
              <td class="mono">100.0%</td>
              <td class="mono">100.0%</td>
              <td class="mono" style="color: var(--primary)">1.000</td>
              <td class="mono" style="color: var(--primary); font-weight: bold;">+0.9540</td>
              <td><span class="tag tag-green">ОТЛИЧНО</span></td>
            </tr>
            <tr>
              <td><strong>Naive Auditor (SimpleRule)</strong></td>
              <td>Наивная политика (ошибки поясов и дат)</td>
              <td class="mono">24</td>
              <td class="mono">88.9%</td>
              <td class="mono">100.0%</td>
              <td class="mono" style="color: var(--amber)">0.889</td>
              <td class="mono" style="color: var(--amber)">+0.7300</td>
              <td><span class="tag tag-amber">ЛОЖНЫЕ ТРЕВОГИ</span></td>
            </tr>
            <tr>
              <td><strong>Flag All (Пометить всё)</strong></td>
              <td>Стресс-тест оценщика (Adversarial attack)</td>
              <td class="mono">100</td>
              <td class="mono">0.0%</td>
              <td class="mono">0.0%</td>
              <td class="mono" style="color: var(--rose)">0.000</td>
              <td class="mono" style="color: var(--rose); font-weight: bold;">-1.0000</td>
              <td><span class="tag tag-rose">АТАКА ОТБИТА</span></td>
            </tr>
          </tbody>
        </table>
      </div>

      <div class="panel-card">
        <div class="panel-header">
          <span class="panel-title">Скрытый эталон оценщика (Ground Truth: truth.json)</span>
          <span class="tag tag-amber">ИЗОЛИРОВАНО ОТ АГЕНТА</span>
        </div>
        <div id="groundTruthContent"></div>
      </div>
    </div>

    <!-- Вкладка 4: Открытые книги -->
    <div id="tab-data" class="tab-pane">
      <div style="display: flex; gap: 8px; margin-bottom: 12px; align-items: center;">
        <button class="btn-primary" onclick="showDataTable('invoices')">Счета (Invoices)</button>
        <button onclick="showDataTable('payments')">Платежи (Payments)</button>
        <button onclick="showDataTable('ledger')">Главная книга (Ledger)</button>
        <button onclick="showDataTable('fx')">Курсы валют (FX)</button>
        <input type="text" id="tableSearchInput" placeholder="Поиск по ID или контрагенту..." oninput="filterDataTable(this.value)" style="margin-left: auto; width: 260px;">
      </div>
      <div class="panel-card" style="padding: 0; overflow-x: auto;">
        <table id="dataTableElement">
          <thead id="dataTableHead"></thead>
          <tbody id="dataTableBody"></tbody>
        </table>
      </div>
    </div>
  </main>

  <footer>
    <div>Audit Gym // Genesis FinTech Hackathon 2026 // Команда The Best Viber Coders</div>
    <div style="font-family: var(--font-mono); font-size: 11px;">100% Offline • Zero-Leakage • Deterministic</div>
  </footer>

  <script>
    // Экранирование данных перед вставкой в HTML: траектории содержат произвольный текст агента
    function money(minor, cur) {
      const ep = window.activeEpisode || {};
      const exps = (ep.manifest && ep.manifest.currency_exponents) || {};
      const exp = (cur in exps) ? exps[cur] : 2;
      const n = Number(minor);
      if (!Number.isFinite(n)) return String(minor);
      const sign = n < 0 ? '−' : '';
      const abs = Math.abs(Math.trunc(n));
      const div = 10 ** exp;
      const whole = Math.floor(abs / div).toString().replace(/\B(?=(\d{3})+(?!\d))/g, ' ');
      const frac = exp > 0 ? ',' + String(abs % div).padStart(exp, '0') : '';
      return `${sign}${whole}${frac}`;
    }

    function esc(v) {
      return String(v ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
    }
    let appData = window.__INITIAL_DATA__ || null;
    let currentEpisodeId = null;
    let currentTrajectory = [];
    let currentStepIdx = 0;
    let playInterval = null;
    let currentDataTableName = 'invoices';

    const TYPE_RU = {
      'duplicate_invoice': 'Дубликат счёта',
      'payment_allocation': 'Неполное распределение платежа',
      'fx_conversion': 'Ошибка курса конвертации',
      'period_cutoff': 'Нарушение периода отсечки'
    };

    const DISTRACTOR_RU = {
      'split_payment': 'Разбитый платёж (частями)',
      'refund': 'Возврат средств (сторно)',
      'legit_recurring': 'Законный регулярный платёж',
      'late_evening_local': 'Поздний вечер (+05:00 vs UTC)'
    };

    function init() {
      if (appData && appData.episodes) {
        populateEpisodesSelect(appData.episodes);
        if (appData.episodes.length > 0) {
          loadEpisode(appData.episodes[0].episode_id);
        }
      } else {
        // Запрос к локальному REST API
        fetch('/api/episodes')
          .then(r => r.json())
          .then(list => {
            populateEpisodesSelect(list);
            if (list.length > 0) {
              loadEpisode(list[0].episode_id);
            }
          })
          .catch(err => {
            console.error('Ошибка загрузки списка эпизодов:', err);
          });
      }
    }

    function populateEpisodesSelect(list) {
      const sel = document.getElementById('episodeSelect');
      sel.innerHTML = '';
      list.forEach(ep => {
        const opt = document.createElement('option');
        opt.value = ep.episode_id;
        opt.textContent = `[${ep.mode.toUpperCase()}] ${ep.episode_id} (${ep.findings_count} находок, rew: ${ep.reward.toFixed(3)})`;
        sel.appendChild(opt);
      });
    }

    function onEpisodeChange(epId) {
      loadEpisode(epId);
    }

    function loadEpisode(epId) {
      currentEpisodeId = epId;
      if (appData && appData.current_episode && appData.current_episode.episode_id === epId) {
        renderEpisodeData(appData.current_episode);
        return;
      }
      fetch('/api/episode?id=' + encodeURIComponent(epId))
        .then(r => r.json())
        .then(data => {
          renderEpisodeData(data);
        })
        .catch(err => console.error(err));
    }

    function renderEpisodeData(data) {
      window.activeEpisode = data;
      currentTrajectory = data.trajectory || [];
      currentStepIdx = 0;

      // Слайдер
      const slider = document.getElementById('stepSlider');
      slider.max = Math.max(0, currentTrajectory.length - 1);
      slider.value = 0;

      // Метрики
      const lastStep = currentTrajectory[currentTrajectory.length - 1] || {};
      const reward = parseFloat(lastStep.reward || 0.0);
      const findings = data.submitted_findings || [];
      const matcher = data.matcher_result;

      if (matcher) {
        const tp = matcher.tp || 0;
        const fp = matcher.fp || 0;
        const fn = matcher.fn || 0;
        const p = (tp + fp) > 0 ? (tp / (tp + fp)) : 0;
        const r = (tp + fn) > 0 ? (tp / (tp + fn)) : 0;
        const f1 = (p + r) > 0 ? ((2 * p * r) / (p + r)) : 0;

        document.getElementById('metricF1').textContent = f1.toFixed(3);
        document.getElementById('metricPR').textContent = `Точность (P): ${(p*100).toFixed(1)}% | Полнота (R): ${(r*100).toFixed(1)}%`;
      } else {
        document.getElementById('metricF1').textContent = findings.length > 0 ? '1.000' : '0.000';
      }

      document.getElementById('metricReward').textContent = (reward >= 0 ? '+' : '') + reward.toFixed(4);
      document.getElementById('metricSteps').innerHTML = `${currentTrajectory.length - 1} <span style="font-size: 13px; font-weight: normal; color: var(--text-muted)">/ 100</span>`;
      document.getElementById('metricStepsLeft').textContent = `Осталось в запасе: ${100 - (currentTrajectory.length - 1)} шагов`;

      // Находки
      renderFindings(data);

      // Эталон
      renderGroundTruth(data);

      // Шаг 0
      renderCurrentStep();

      // Таблицы
      showDataTable(currentDataTableName);
    }

    function renderCurrentStep() {
      const step = currentTrajectory[currentStepIdx];
      const slider = document.getElementById('stepSlider');
      slider.value = currentStepIdx;

      document.getElementById('stepCounterLabel').textContent = `Шаг ${currentStepIdx} / ${Math.max(0, currentTrajectory.length - 1)}`;

      if (!step) return;

      const act = step.action;
      const obs = step.observation || {};

      // Бейджи
      const tool = act ? act.tool : 'RESET';
      const toolBadge = document.getElementById('stepToolBadge');
      toolBadge.textContent = tool.toUpperCase();
      if (tool === 'submit') {
        toolBadge.className = 'tag tag-green';
      } else if (tool === 'get_document') {
        toolBadge.className = 'tag tag-amber';
      } else {
        toolBadge.className = 'tag tag-blue';
      }

      const obsKind = obs.kind || 'UNKNOWN';
      const obsBadge = document.getElementById('stepObsKindBadge');
      obsBadge.textContent = obsKind.toUpperCase();

      // Содержимое Action
      const actContent = document.getElementById('stepActionContent');
      if (!act) {
        actContent.innerHTML = `<p style="color: var(--text-muted)">Начальный сброс среды (env.reset). Агент получает задание и открытый манифест компании.</p>`;
      } else {
        let html = `<div style="margin-bottom: 8px;"><strong>Вызов инструмента:</strong> <code class="mono" style="color: var(--primary)">${esc(act.tool)}(...)</code></div>`;
        html += `<pre>${esc(JSON.stringify(act.args, null, 2))}</pre>`;
        actContent.innerHTML = html;
      }

      // Содержимое Observation
      const obsContent = document.getElementById('stepObservationContent');
      if (obs.kind === 'initial') {
        const m = obs.manifest || {};
        obsContent.innerHTML = `
          <div style="display: flex; flex-direction: column; gap: 8px;">
            <div><strong>Эпизод:</strong> <code class="mono">${esc(m.episode_id || '')}</code></div>
            <div><strong>Базовая валюта:</strong> <span class="tag tag-green">${esc(m.base_currency)}</span> | <strong>Пояс:</strong> ${esc(m.timezone)}</div>
            <div><strong>Период:</strong> ${esc(m.period_start?.slice(0, 10))} — ${esc(m.period_end?.slice(0, 10))}</div>
            <div><strong>Масштаб данных:</strong> ${esc(m.counts?.invoices)} счетов, ${esc(m.counts?.payments)} платежей, ${esc(m.counts?.ledger_rows)} проводок книги</div>
          </div>
        `;
      } else if (obs.kind === 'result') {
        const res = obs.result || {};
        if (obs.tool === 'list_records') {
          const recs = res.records || [];
          obsContent.innerHTML = `
            <div><strong>Загружено записей:</strong> ${recs.length} из ${esc(res.total)} (таблица: <span class="tag tag-blue">${esc(res.table)}</span>)</div>
            <div style="font-size: 11px; color: var(--text-muted); margin: 6px 0;">Следующий курсор: <code class="mono">${esc(res.next_cursor || 'завершено')}</code></div>
            <pre style="max-height: 200px;">${esc(JSON.stringify(recs.slice(0, 3), null, 2))}${recs.length > 3 ? '\n... и ещё ' + (recs.length - 3) + ' записей' : ''}</pre>
          `;
        } else if (obs.tool === 'get_document') {
          const doc = res.document || {};
          let linesHtml = '';
          if (doc.line_items) {
            linesHtml = `<table style="margin-top: 8px;">
              <thead><tr><th>Позиция</th><th>Кол-во</th><th>Цена</th><th>Сумма</th></tr></thead>
              <tbody>${doc.line_items.map(li => `<tr><td>${esc(li.description)}</td><td class="mono">${esc(li.quantity)}</td><td class="mono">${esc(money(li.unit_price_minor, doc.currency))}</td><td class="mono">${esc(money(li.amount_minor, doc.currency))}</td></tr>`).join('')}</tbody>
            </table>`;
          }
          obsContent.innerHTML = `
            <div><strong>Первичный документ:</strong> <code class="mono">${esc(doc.doc_id)}</code> (${esc(doc.doc_type || 'invoice')})</div>
            <div><strong>Контрагент:</strong> ${esc(doc.entity_id)} | <strong>Сумма:</strong> <span class="mono" style="color: var(--primary); font-weight: bold">${esc(money(doc.total_minor ?? doc.amount_minor, doc.currency))} ${esc(doc.currency)}</span></div>
            ${linesHtml}
          `;
        } else if (obs.tool === 'calculator') {
          obsContent.innerHTML = `
            <div><strong>Выражение:</strong> <code class="mono">${esc(res.expression)}</code></div>
            <div style="margin-top: 8px;"><strong>Результат (Decimal AST):</strong> <span class="tag tag-green mono" style="font-size: 14px;">${esc(res.value)}</span></div>
          `;
        } else {
          obsContent.innerHTML = `<pre>${esc(JSON.stringify(res, null, 2))}</pre>`;
        }
      } else if (obs.kind === 'final') {
        obsContent.innerHTML = `
          <div style="text-align: center; padding: 20px 0;">
            <div style="font-size: 24px; font-weight: 800; color: var(--primary)">ЭПИЗОД ЗАВЕРШЁН</div>
            <div style="margin-top: 8px; color: var(--text-muted)">Статус: <span class="tag tag-green">${esc(obs.status)}</span> | Использовано шагов: <strong>${esc(obs.steps_used)}</strong> / 100</div>
          </div>
        `;
      }
    }

    function renderFindings(data) {
      const grid = document.getElementById('findingsGrid');
      grid.innerHTML = '';
      const list = data.submitted_findings || [];
      document.getElementById('findingsTotalBadge').textContent = `${list.length} находок`;

      const matcher = data.matcher_result;
      const matchedMap = {};
      if (matcher && matcher.matches) {
        matcher.matches.forEach(m => { matchedMap[m.finding_index] = m.issue_id; });
      }

      list.forEach((f, idx) => {
        const isMatched = matcher ? (idx in matchedMap) : true;
        const card = document.createElement('div');
        card.className = `finding-card ${isMatched ? 'tp' : 'fp'}`;

        const typeRu = TYPE_RU[f.type] || f.type;
        const matchBadge = matcher ? (isMatched ? `<span class="tag tag-green">TP: ${esc(matchedMap[idx])}</span>` : `<span class="tag tag-rose">FP: Не совпало</span>`) : '';

        card.innerHTML = `
          <div class="finding-header">
            <span class="finding-type">${esc(typeRu)}</span>
            ${matchBadge}
          </div>
          <div class="finding-amount">${esc(money(f.claimed_amount_minor, f.currency))} ${esc(f.currency)}</div>
          <div class="finding-meta"><strong>Затронуто:</strong> ${esc((f.affected_ids || []).join(', '))}</div>
          <div class="finding-meta" style="color: var(--text-muted); font-style: italic;">${esc(f.explanation)}</div>
          <div class="pills-container">
            ${(f.evidence_ids || []).map(e => `<span class="pill">${esc(e)}</span>`).join('')}
          </div>
        `;
        grid.appendChild(card);
      });
    }

    function renderGroundTruth(data) {
      const container = document.getElementById('groundTruthContent');
      const truth = data.truth;
      if (!truth || !truth.issues) {
        container.innerHTML = `<p style="color: var(--text-muted)">Эталон недоступен (изолированный eval-режим).</p>`;
        return;
      }
      let html = `<table style="margin-bottom: 16px;">
        <thead><tr><th>Issue ID</th><th>Тип ошибки</th><th>Ожидаемая сумма</th><th>Затронутые ID</th><th>Допустимые доказательства</th></tr></thead>
        <tbody>
          ${truth.issues.map(iss => `
            <tr>
              <td class="mono"><strong>${esc(iss.issue_id)}</strong></td>
              <td>${esc(TYPE_RU[iss.type] || iss.type)}</td>
              <td class="mono">${esc(money(iss.expected_amount_minor, iss.currency))} ${esc(iss.currency)}</td>
              <td class="mono">${esc((iss.affected_ids || []).join(', '))}</td>
              <td class="mono" style="font-size: 11px;">${iss.evidence_sets.map(s => `[${esc(s.join(', '))}]`).join(' OR ')}</td>
            </tr>
          `).join('')}
        </tbody>
      </table>`;

      if (truth.distractors && truth.distractors.length > 0) {
        html += `<div style="font-weight: 700; font-size: 12px; margin-bottom: 6px; color: var(--text-muted)">ОТВЛЕКАЮЩИЕ СИТУАЦИИ (DISTRACTORS):</div>`;
        html += `<table>
          <thead><tr><th>Distractor ID</th><th>Тип ловушки</th><th>Записи компании</th></tr></thead>
          <tbody>
            ${truth.distractors.map(d => `
              <tr>
                <td class="mono">${esc(d.distractor_id)}</td>
                <td><span class="tag tag-amber">${esc(DISTRACTOR_RU[d.kind] || d.kind)}</span></td>
                <td class="mono">${esc((d.record_ids || []).join(', '))}</td>
              </tr>
            `).join('')}
          </tbody>
        </table>`;
      }
      container.innerHTML = html;
    }

    function showDataTable(tableName) {
      currentDataTableName = tableName;
      const data = window.activeEpisode;
      if (!data) return;

      const thead = document.getElementById('dataTableHead');
      const tbody = document.getElementById('dataTableBody');
      thead.innerHTML = '';
      tbody.innerHTML = '';

      const rows = data[tableName] || [];
      if (rows.length === 0) {
        tbody.innerHTML = `<tr><td style="color: var(--text-muted)">Нет данных в таблице ${esc(tableName)}</td></tr>`;
        return;
      }

      const keys = Object.keys(rows[0]);
      thead.innerHTML = `<tr>${keys.map(k => `<th>${esc(k)}</th>`).join('')}</tr>`;

      const displayRows = rows.slice(0, 50);
      tbody.innerHTML = displayRows.map(r => `
        <tr>${keys.map(k => {
          let val = r[k];
          if (typeof val === 'object' && val !== null) val = JSON.stringify(val);
          return `<td class="mono">${val !== null ? esc(val) : 'null'}</td>`;
        }).join('')}</tr>
      `).join('');
    }

    function filterDataTable(q) {
      const qLower = q.toLowerCase();
      const rows = document.querySelectorAll('#dataTableBody tr');
      rows.forEach(r => {
        r.style.display = r.textContent.toLowerCase().includes(qLower) ? '' : 'none';
      });
    }

    function switchTab(tabName) {
      document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
      document.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('active'));
      document.querySelector(`[onclick="switchTab('${tabName}')"]`).classList.add('active');
      document.getElementById(`tab-${tabName}`).classList.add('active');
    }

    // Управление плеером
    function playerFirst() { currentStepIdx = 0; renderCurrentStep(); }
    function playerPrev() { if (currentStepIdx > 0) { currentStepIdx--; renderCurrentStep(); } }
    function playerNext() { if (currentStepIdx < currentTrajectory.length - 1) { currentStepIdx++; renderCurrentStep(); } }
    function playerLast() { currentStepIdx = Math.max(0, currentTrajectory.length - 1); renderCurrentStep(); }
    function onSliderSeek(val) { currentStepIdx = parseInt(val, 10); renderCurrentStep(); }

    function togglePlay() {
      const btn = document.getElementById('playBtn');
      if (playInterval) {
        clearInterval(playInterval);
        playInterval = null;
        btn.textContent = '▶ Плей';
        btn.className = 'btn-primary';
      } else {
        btn.textContent = '⏸ Пауза';
        btn.className = '';
        const speed = parseInt(document.getElementById('playSpeed').value, 10) || 500;
        playInterval = setInterval(() => {
          if (currentStepIdx < currentTrajectory.length - 1) {
            currentStepIdx++;
            renderCurrentStep();
          } else {
            togglePlay();
          }
        }, speed);
      }
    }

    function runSelectedPolicy() {
      const policy = document.getElementById('policySelect').value;
      const epId = currentEpisodeId;
      const btn = event.currentTarget;
      const origText = btn.innerHTML;
      btn.innerHTML = 'Запуск...';
      btn.disabled = true;

      fetch('/api/run', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ episode_id: epId, policy: policy })
      })
      .then(r => r.json())
      .then(res => {
        btn.innerHTML = origText;
        btn.disabled = false;
        if (res.trajectory) {
          window.activeEpisode.trajectory = res.trajectory;
          window.activeEpisode.submitted_findings = res.submitted_findings;
          // сопоставление с эталоном из сервера: без matches все верные находки выглядели как FP
          window.activeEpisode.matcher_result = res.matcher_result || null;
          renderEpisodeData(window.activeEpisode);
        }
      })
      .catch(err => {
        btn.innerHTML = origText;
        btn.disabled = false;
        alert('Ошибка запуска: ' + err);
      });
    }

    function generateRandomSeed() {
      const seed = Math.floor(Math.random() * 900000) + 100000;
      const btn = event.currentTarget;
      btn.disabled = true;
      btn.textContent = 'Генерация...';

      fetch('/api/generate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ seed: seed })
      })
      .then(r => r.json())
      .then(res => {
        btn.disabled = false;
        btn.textContent = '🎲 Случайный seed';
        if (res.episode_id) {
          fetch('/api/episodes')
            .then(r => r.json())
            .then(list => {
              populateEpisodesSelect(list);
              document.getElementById('episodeSelect').value = res.episode_id;
              loadEpisode(res.episode_id);
            });
        }
      })
      .catch(err => {
        btn.disabled = false;
        btn.textContent = '🎲 Случайный seed';
        alert('Ошибка генерации: ' + err);
      });
    }

    document.addEventListener('DOMContentLoaded', init);
  </script>
</body>
</html>
"""


POLICIES = ("oracle", "smart", "naive", "flag_all", "random")
_EPISODE_ID = re.compile(r"EP-[0-9A-HJKMNP-TV-Z]{8}")
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}
MAX_BODY = 64 * 1024


def _hostname(host_header: str) -> str:
    """Имя хоста из заголовка Host без порта (поддерживает [::1]:8080)."""
    host = host_header.strip()
    if host.startswith("["):
        return host[1:].split("]", 1)[0]
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


def _host_allowed(bound: str, host_header: str) -> bool:
    """Host: loopback или тот IP, на котором явно запущен сервер (--host 192.168.1.5).
    При --host 0.0.0.0 API доступен только через localhost: после DNS rebinding в Host
    стоит домен злоумышленника, а не IP, поэтому такой запрос не пройдёт."""
    name = _hostname(host_header)
    return name in _LOOPBACK or (bound not in ("0.0.0.0", "::", "") and name == bound)


class AuditUIRequestHandler(http.server.BaseHTTPRequestHandler):
    """Легковесный HTTP-обработчик для UI и REST API."""

    def _request_allowed(self) -> bool:
        """API только для страниц самого дашборда: чужой Origin и подмена Host (DNS rebinding) запрещены."""
        host = self.headers.get("Host") or ""
        if not _host_allowed(self.server.server_address[0], host):
            return False
        origin = self.headers.get("Origin")
        return origin is None or origin == "http://" + host

    def _forbid(self):
        self.send_error(403, "Forbidden")

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        if path == "/" or path == "/index.html":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(HTML_TEMPLATE.encode("utf-8"))
            return

        if path.startswith("/api/") and not self._request_allowed():
            self._forbid()
            return

        if path == "/api/episodes":
            episodes = get_available_episodes()
            self.send_json(episodes)
            return

        elif path == "/api/episode":
            ep_id = query.get("id", [""])[0]
            # только ID эпизода из episodes/, никаких путей и шаблонов glob
            details = get_episode_details(ep_id) if _EPISODE_ID.fullmatch(ep_id) else None
            if details:
                self.send_json(details)
            else:
                self.send_error(404, "Episode not found")
            return

        self.send_error(404, "Not Found")

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if not self._request_allowed():
            self._forbid()
            return
        try:
            content_length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            content_length = -1
        if not 0 <= content_length <= MAX_BODY:
            self.send_error(413, "Request too large")
            return
        post_data = self.rfile.read(content_length)

        try:
            req_json = json.loads(post_data.decode("utf-8"))
        except Exception:
            req_json = {}
        if not isinstance(req_json, dict):
            req_json = {}

        if path == "/api/run":
            ep_id = req_json.get("episode_id")
            policy = req_json.get("policy", "smart")
            if not isinstance(ep_id, str) or not _EPISODE_ID.fullmatch(ep_id):
                self.send_error(404, "Episode not found")
                return
            if not isinstance(policy, str) or policy.lower() not in POLICIES:
                self.send_error(400, "Unknown policy")
                return
            details = get_episode_details(ep_id)
            if not details:
                self.send_error(404, "Episode not found")
                return
            ep_path = find_episode_path(ep_id)
            if not ep_path:
                self.send_error(404, "Episode path not found")
                return
            result = run_policy_on_episode(ep_path, policy)
            self.send_json(result)
            return

        elif path == "/api/generate":
            try:
                seed = int(req_json.get("seed", 42))
            except (TypeError, ValueError):
                self.send_error(400, "Bad seed")
                return
            try:
                out_dir = ROOT / "episodes" / "train"
                out_dir.mkdir(parents=True, exist_ok=True)
                ep_path = generate_episode(seed, TRAIN_CONFIG, out_dir)
                # Запускаем Smart Auditor для записи первой траектории
                from scripts.collect_episodes import run_episode_and_record_trajectory
                policy = RuleBasedPolicy(naive=False)
                traj = run_episode_and_record_trajectory(ep_path, TRAIN_CONFIG, policy)
                with open(ep_path / "trajectories.jsonl", "w", encoding="utf-8", newline="\n") as f:
                    for item in traj:
                        f.write(canonical_dumps(item) + "\n")
            except Exception:
                traceback.print_exc(file=sys.stderr)
                self.send_error(500, "Generation failed")
                return
            self.send_json({"episode_id": ep_path.name, "seed": seed})
            return

        self.send_error(404, "Not Found")

    def send_json(self, data: Any):
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format, *args):
        # Тихий режим (без спама логами каждого GET)
        pass


def find_episode_path(episode_id: str) -> Optional[Path]:
    for p in [
        ROOT / "episodes" / "train" / episode_id,
        ROOT / "episodes" / "dev" / episode_id,
        ROOT / "episodes" / episode_id,
    ]:
        if p.exists() and p.is_dir():
            return p
    matches = list((ROOT / "episodes").glob(f"**/{episode_id}"))
    if matches:
        return matches[0]
    return None


def export_standalone_html(output_path: Path, episode_id: Optional[str] = None) -> None:
    """Генерирует полностью автономный HTML-файл с вшитыми данными (открывается двойным кликом)."""
    episodes = get_available_episodes()
    target_id = episode_id or (episodes[0]["episode_id"] if episodes else None)
    current_ep = get_episode_details(target_id) if target_id else None

    initial_data = {
        "episodes": episodes,
        "current_episode": current_ep,
    }

    # "<" бывает только внутри строк JSON; \u003c не даёт "</script>" и "<!--" сломать тег <script>
    payload = json.dumps(initial_data, ensure_ascii=False).replace("<", "\\u003c")
    embedded_script = f"\n<script>window.__INITIAL_DATA__ = {payload};</script>\n"
    html_out = HTML_TEMPLATE.replace("<body>", f"<body>{embedded_script}")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html_out, encoding="utf-8")
    print(f"[OK] Автономный отчёт успешно экспортирован: {output_path.resolve()}")


def serve(host: str = "127.0.0.1", port: int = 8080, open_browser: bool = False):
    """Запускает веб-сервер UI."""
    actual_port = find_free_port(port)
    server_address = (host, actual_port)
    httpd = http.server.HTTPServer(server_address, AuditUIRequestHandler)
    # при 0.0.0.0 API отвечает только через localhost (см. _host_allowed)
    url_host = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
    url = f"http://{url_host}:{actual_port}"
    if host not in _LOOPBACK:
        print(f"[ВНИМАНИЕ] Дашборд показывает эталон (truth) и доступен из сети на {host}. "
              "Используйте только в доверенной сети.", file=sys.stderr)

    print("=" * 70)
    print("  AUDIT GYM // ВЕБ-ИНТЕРФЕЙС ФИНАНСОВОГО АУДИТОРА (UI)")
    print("=" * 70)
    print(f"  Локальный адрес дашборда: {url}")
    print("  100% Offline • Без внешних CDN • Соответствует регламенту FinTech")
    print("  Для остановки сервера нажмите Ctrl+C\n")

    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[OK] Сервер остановлен пользователем.")
        httpd.server_close()


def _utf8_stdio() -> None:
    """Вывод в UTF-8 и при перенаправлении в pipe на Windows (cp1251 не знает рамок и стрелок)."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")


def main():
    _utf8_stdio()
    parser = argparse.ArgumentParser(description="Минималистичный UI для Audit Gym")
    parser.add_argument("--port", type=int, default=8080, help="Порт сервера (по умолчанию 8080)")
    parser.add_argument("--host", default="127.0.0.1", help="Хост сервера")
    parser.add_argument("--open", action="store_true", help="Автоматически открыть браузер")
    parser.add_argument("--export", type=str, default=None, help="Экспорт автономного HTML файла (например, docs/ui.html)")
    parser.add_argument("--episode", type=str, default=None, help="Целевой эпизод для экспорта")
    args = parser.parse_args()

    if args.export:
        export_standalone_html(Path(args.export), args.episode)
        return 0

    serve(host=args.host, port=args.port, open_browser=args.open)
    return 0


if __name__ == "__main__":
    sys.exit(main())
