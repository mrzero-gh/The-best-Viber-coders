# Audit Gym: воспроизводимая проверка без сети.
#   docker build -t audit-gym .
#   docker run --rm --network none --read-only --tmpfs /tmp audit-gym
# Или одной командой: python -m scripts.check --docker

# Базовый образ закреплён по версии и digest: у жюри соберётся ровно тот же Python 3.11.18
FROM python:3.11.18-slim@sha256:07d4824036af339fa1b6ef769cc99d9c10a141769538b52b3b597c84f7a7b535

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONHASHSEED=0 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTEST_ADDOPTS="-p no:cacheprovider" \
    HYPOTHESIS_STORAGE_DIRECTORY=/tmp/hypothesis \
    AUDIT_GYM_RUNS_DIR=/tmp/runs

WORKDIR /app

# Зависимости отдельным слоем: точные версии, сеть нужна только на этапе сборки
COPY requirements.txt requirements-dev.txt ./
RUN pip install -r requirements.txt -r requirements-dev.txt

COPY . .

# Проверка идёт не от root: код и эпизоды доступны только для чтения
RUN useradd --create-home --uid 10001 auditor
USER auditor

CMD ["python", "-m", "scripts.check"]
