# KubeSight agent platform: one image, two roles.
#   API    uvicorn agent_platform.api:app --host 0.0.0.0 --port 8000
#   worker python -m agent_platform.worker
# The department file and prompts are baked in for a local run; the chart
# mounts its own copy over /etc/agents.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYDANTIC_AI_NO_BANNER=1 \
    AGENTS_CONFIG=/etc/agents/agents.yaml

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY agent_platform/ agent_platform/
COPY departments/infra/ /etc/agents/

RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin agents
USER 10001

EXPOSE 8000
CMD ["uvicorn", "agent_platform.api:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
