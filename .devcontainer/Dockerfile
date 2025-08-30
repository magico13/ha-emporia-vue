FROM mcr.microsoft.com/devcontainers/python:3.11

# Install OS deps commonly needed by HA & integrations
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    libxml2-dev libxslt1-dev libssl-dev \
    libjpeg-dev zlib1g-dev tzdata && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /workspace

# Copy requirements and install them
COPY requirements_dev.txt .

RUN pip install --no-cache-dir -r requirements_dev.txt

USER vscode