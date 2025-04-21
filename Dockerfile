FROM python:3.13-slim-bookworm

# Setup env
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONFAULTHANDLER=1

ARG DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install --no-install-recommends --no-install-suggests -y \
        curl \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home bot

WORKDIR /home/bot

USER bot

RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH=$PATH:/home/bot/.local/bin

COPY pyproject.toml tacticus-guild-raid.py uv.lock ./

RUN uv sync

ENTRYPOINT [ "uv", "run", "tacticus-guild-raid.py" ] 
