FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends git curl ca-certificates bash unzip \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://opencode.ai/install | bash
ENV PATH="/root/.opencode/bin:${PATH}"

WORKDIR /opt/tribe-agent
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

WORKDIR /workspace

ENTRYPOINT ["tribe-agent"]
CMD ["--config", "/workspace/config.yaml", "run"]
