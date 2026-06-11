FROM python:3.11-slim

# Install Chromium and dependencies
RUN apt-get update && apt-get install -y \
    chromium \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libdbus-1-3 libxkbcommon0 \
    libx11-6 libxcomposite1 libxdamage1 libxext6 \
    libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 \
    libcairo2 libasound2 libxshmfence1 \
    --no-install-recommends && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
# Resilient pip install — Railway's build host has hit transient PyPI
# connection drops that exhausted pip's default 5 retries with 15 s
# timeouts, failing the build with a misleading "no matching
# distribution" error. Bump retries modestly and use a 45 s socket
# timeout (long enough to ride out a brief drop, short enough that
# the WORST-case total per-package wait stays under ~7 min even if
# every retry exhausts). Pin the official pypi.org index URL.
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir \
        --retries 8 \
        --timeout 45 \
        --index-url https://pypi.org/simple \
        -r requirements.txt

# Install playwright and point it to system chromium
RUN playwright install chromium || true

COPY . .

RUN python manage.py collectstatic --noinput || true

ENV PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH=/usr/bin/chromium
ENV PYTHONUNBUFFERED=1

EXPOSE 8080

RUN chmod +x startup.sh
CMD ["./startup.sh"]
