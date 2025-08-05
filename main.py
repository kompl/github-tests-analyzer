import os
import re
import requests
import zipfile
import io
from pathlib import Path
from datetime import datetime

# Конфигурация
GITHUB_TOKEN = os.getenv('GITHUB_TOKEN')  # Personal Access Token
OWNER = 'hydra-billing'
REPO = 'hoper'  # или 'hydra-server'
BRANCH = 'v6.2'             # Анализируемая ветка
MASTER_BRANCH = 'master'      # Для сравнения
WORKFLOW_FILE = 'ci.yml'      # Только ci.yml
MAX_RUNS = 10
SAVE_LOGS = False
OUTPUT_DIR = Path('downloaded_logs')

HEADERS = {
    'Authorization': f'token {GITHUB_TOKEN}',
    'Accept': 'application/vnd.github.v3+json'
}

def get_recent_runs(owner, repo, branch, workflow_file, max_runs):
    runs = []
    page = 1
    while len(runs) < max_runs:
        url = f'https://api.github.com/repos/{owner}/{repo}/actions/workflows/{workflow_file}/runs'
        params = {'branch': branch, 'per_page': max_runs, 'page': page}
        resp = requests.get(url, headers=HEADERS, params=params)
        if resp.status_code != 200:
            print(f"Ошибка при получении runs: {resp.status_code} - {resp.text}")
            break
        items = resp.json().get('workflow_runs', [])
        if not items:
            break
        for run in items:
            runs.append({
                'id': run['id'],
                'sha': run['head_sha'],
                'timestamp': run.get('run_started_at') or run.get('created_at'),
                'status': run['status'],
                'conclusion': run.get('conclusion')
            })
            if len(runs) >= max_runs:
                break
        page += 1
    runs.sort(key=lambda x: datetime.fromisoformat(x['timestamp'].replace('Z', '+00:00')))
    return runs

def get_latest_completed_run(owner, repo, branch, workflow_file):
    url = f'https://api.github.com/repos/{owner}/{repo}/actions/workflows/{workflow_file}/runs'
    params = {'branch': branch, 'per_page': MAX_RUNS}
    resp = requests.get(url, headers=HEADERS, params=params)
    if resp.status_code != 200:
        print(f"Ошибка при получении runs для ветки {branch}: {resp.status_code} - {resp.text}")
        return None
    items = resp.json().get('workflow_runs', [])
    if not items:
        return None

    items.sort(key=lambda x: datetime.fromisoformat((x.get('run_started_at') or x.get('created_at')).replace('Z', '+00:00')), reverse=True)

    for run in items:
        status = run['status']
        conclusion = run.get('conclusion')
        if status == 'completed' and conclusion in ['success', 'failure']:
            print(f"Найден run в '{branch}': ID {run['id']}, status: {status}, conclusion: {conclusion}")
            return {
                'id': run['id'],
                'sha': run['head_sha'],
                'timestamp': run.get('run_started_at') or run.get('created_at'),
                'status': status,
                'conclusion': conclusion
            }
        else:
            print(f"Пропущен run ID {run['id']} — статус: {status}, conclusion: {conclusion}")
    return None

def get_commit_message(owner, repo, sha):
    url = f'https://api.github.com/repos/{owner}/{repo}/commits/{sha}'
    resp = requests.get(url, headers=HEADERS)
    if resp.status_code != 200:
        print(f"Ошибка при получении коммита {sha}: {resp.status_code} - {resp.text}")
        return "Недоступно"
    data = resp.json()
    return data.get('commit', {}).get('message', '').splitlines()[0]

def download_logs_bytes(owner, repo, run_id):
    url = f'https://api.github.com/repos/{owner}/{repo}/actions/runs/{run_id}/logs'
    try:
        resp = requests.get(url, headers=HEADERS)
        resp.raise_for_status()
        return resp.content
    except requests.exceptions.HTTPError as e:
        print(f"Ошибка при загрузке логов run {run_id}: {e}")
        return None

def save_logs(zip_bytes, sha, run_id):
    run_dir = OUTPUT_DIR / sha
    run_dir.mkdir(parents=True, exist_ok=True)
    zip_path = run_dir / f'logs_{run_id}.zip'
    with open(zip_path, 'wb') as f:
        f.write(zip_bytes)
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
        z.extractall(path=run_dir)

def parse_failed_tests(zip_bytes):
    result = set()
    pattern = re.compile(r'🧪\s*-\s*(.*?)\s*\|')
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
        for name in z.namelist():
            if not name.lower().endswith('.txt'):
                continue
            with z.open(name) as f:
                for raw in f:
                    line = raw.decode('utf-8', errors='ignore')
                    match = pattern.search(line)
                    if match:
                        result.add(match.group(1).strip())
    return result

def main():
    if not GITHUB_TOKEN:
        print("❌ Ошибка: переменная окружения GITHUB_TOKEN не задана.")
        return

    print(f"📦 Получение последнего завершённого запуска '{WORKFLOW_FILE}' в ветке '{MASTER_BRANCH}'...")
    master_run = get_latest_completed_run(OWNER, REPO, MASTER_BRANCH, WORKFLOW_FILE)
    if master_run:
        zip_bytes = download_logs_bytes(OWNER, REPO, master_run['id'])
        master_failed = parse_failed_tests(zip_bytes) if zip_bytes else set()
        print(f"✅ В master обнаружено {len(master_failed)} падающих тестов.")
    else:
        print("⚠ Не удалось получить валидный запуск в master — падения отсутствуют.")
        master_failed = set()

    runs = get_recent_runs(OWNER, REPO, BRANCH, WORKFLOW_FILE, MAX_RUNS)
    if not runs:
        print(f"❌ Нет запусков '{WORKFLOW_FILE}' в ветке '{BRANCH}'")
        return

    summary, timestamps, messages, statuses = {}, {}, {}, {}

    for run in runs:
        rid, sha = run['id'], run['sha']
        ts = datetime.fromisoformat(run['timestamp'].replace('Z', '+00:00')).strftime('%Y-%m-%d %H:%M:%S')
        status = run['status']
        conclusion = run.get('conclusion') or '—'
        msg = get_commit_message(OWNER, REPO, sha)
        zip_bytes = download_logs_bytes(OWNER, REPO, rid)
        failed = parse_failed_tests(zip_bytes) if zip_bytes else set()

        summary[sha] = failed
        timestamps[sha] = ts
        messages[sha] = msg
        statuses[sha] = f"{status}/{conclusion}"

        print(f"🔍 Обработка {sha} | Ветка: {BRANCH} | {ts} | {msg} | Статус: {statuses[sha]}")

    prev = set()
    print("\n=== 📊 Изменения падений тестов по последним запускам ===")
    for sha, curr in summary.items():
        added = curr - prev
        removed = prev - curr
        print(f"\n📦 Запуск: {sha} | Ветка: {BRANCH} | {timestamps[sha]} | {messages[sha]} | Статус: {statuses[sha]}")
        if added:
            print("➕ Новые падения:")
            for path in sorted(added):
                marker = " (также падает в master)" if path in master_failed else " (не падает в master)"
                print(f"    {path}{marker}")
        else:
            print("➕ Новые падения: нет")

        if removed:
            print("✔ Починились:")
            for path in sorted(removed):
                print(f"    {path}")
        else:
            print("✔ Починились: нет")

        only_here = curr - master_failed
        if only_here:
            print("⚠ Уникальные падения в текущей ветке (не падают в master):")
            for path in sorted(only_here):
                print(f"    {path}")
        else:
            print("⚠ Уникальные падения в текущей ветке: нет")

        prev = curr

if __name__ == '__main__':
    main()
