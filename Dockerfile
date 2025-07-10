# STAGE 1: Builder
FROM python:3.11-slim AS builder

# Install build-time dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    make \
    && rm -rf /var/lib/apt/lists/*

# Create and activate virtual environment
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Install Python packages
COPY requirements.txt /tmp/
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r /tmp/requirements.txt

# STAGE 2: Production
FROM python:3.11-slim

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DEBIAN_FRONTEND=noninteractive \
    PATH="/opt/venv/bin:$PATH" \
    DOCKER_CONTAINER=1 \
    DISPLAY=:99 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# Install runtime system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    gosu \
    supervisor \
    xvfb \
    x11vnc \
    fonts-liberation \
    fonts-noto-color-emoji \
    procps \
    psmisc \
    && rm -rf /var/lib/apt/lists/*

# Copy virtual environment from builder
COPY --from=builder /opt/venv /opt/venv

# Install Playwright and browser dependencies
RUN pip install playwright && \
    playwright install-deps chromium

# Create non-root user
RUN groupadd -r appuser && \
    useradd -r -g appuser -d /app -s /bin/bash -c "App user" appuser

# Create directories
RUN mkdir -p \
    /app/tmp/logs \
    /app/tmp/sessions \
    /app/tmp/downloads \
    /app/tmp/recordings \
    /app/tmp/traces \
    /app/static \
    /var/log/supervisor \
    /var/run/supervisor \
    /ms-playwright

# Set working directory
WORKDIR /app

# Copy application code and set ownership
COPY --chown=appuser:appuser . .

# Set permissions
RUN chown -R appuser:appuser /app && \
    chown -R appuser:appuser /ms-playwright && \
    chmod -R 755 /app/tmp

# Switch to appuser to install browser
USER appuser
RUN playwright install chromium

# Switch back to root for final setup
USER root

# Create debugging entrypoint script
RUN printf '#!/bin/bash\n\
set -e\n\
\n\
echo "=== Starting Multi-User Browser Automation Service ==="\n\
echo "Environment: $(env | grep -E '"'"'(PYTHON|PATH|DISPLAY)'"'"' | sort)"\n\
echo "User: $(whoami)"\n\
echo "Working directory: $(pwd)"\n\
echo "Python location: $(which python)"\n\
echo "Python version: $(python --version)"\n\
\n\
export DISPLAY=:99\n\
export RESOLUTION=${RESOLUTION:-1920x1080x24}\n\
\n\
check_process() {\n\
    if ! kill -0 $1 2>/dev/null; then\n\
        return 1\n\
    fi\n\
    return 0\n\
}\n\
\n\
echo "Starting virtual display..."\n\
Xvfb :99 -screen 0 $RESOLUTION -ac +extension GLX +render -noreset > /var/log/Xvfb.log 2>&1 &\n\
XVFB_PID=$!\n\
\n\
echo "Waiting for virtual display to be ready..."\n\
for i in {1..10}; do\n\
    if check_process $XVFB_PID; then\n\
        echo "Virtual display started successfully"\n\
        break\n\
    fi\n\
    if [ $i -eq 10 ]; then\n\
        echo "ERROR: Xvfb failed to start properly"\n\
        if [ -f /var/log/Xvfb.log ]; then\n\
            cat /var/log/Xvfb.log\n\
        fi\n\
        exit 1\n\
    fi\n\
    sleep 1\n\
done\n\
\n\
echo "=== System Component Checks ==="\n\
\n\
echo "Testing Python imports as appuser..."\n\
if ! gosu appuser python -c "import sys; print(f'"'"'Python path: {sys.path}'"'"')"; then\n\
    echo "ERROR: Python not working for appuser"\n\
    exit 1\n\
fi\n\
\n\
if ! gosu appuser python -c "import playwright; print('"'"'✓ Playwright available'"'"')"; then\n\
    echo "ERROR: Playwright not properly installed"\n\
    exit 1\n\
fi\n\
\n\
echo "Testing main application imports..."\n\
if ! gosu appuser python -c "import fastapi; print('"'"'✓ FastAPI available'"'"')"; then\n\
    echo "ERROR: FastAPI not available"\n\
    exit 1\n\
fi\n\
\n\
if ! gosu appuser python -c "import uvicorn; print('"'"'✓ Uvicorn available'"'"')"; then\n\
    echo "ERROR: Uvicorn not available"\n\
    exit 1\n\
fi\n\
\n\
echo "Testing application structure..."\n\
gosu appuser ls -la /app/ || echo "Cannot list /app/"\n\
\n\
echo "Testing main.py import..."\n\
if ! gosu appuser python -c "import main; print('"'"'✓ main.py imports successfully'"'"')"; then\n\
    echo "ERROR: Cannot import main.py"\n\
    echo "Attempting to show error:"\n\
    gosu appuser python -c "import main" 2>&1 || true\n\
    exit 1\n\
fi\n\
\n\
echo "Testing webui.py import..."\n\
if ! gosu appuser python -c "import webui; print('"'"'✓ webui.py imports successfully'"'"')"; then\n\
    echo "ERROR: Cannot import webui.py"\n\
    echo "Attempting to show error:"\n\
    gosu appuser python -c "import webui" 2>&1 || true\n\
    echo "WARNING: webui.py has import issues but continuing..."\n\
fi\n\
\n\
echo "✓ All system checks passed. Starting application..."\n\
\n\
chown -R appuser:appuser /app/tmp /ms-playwright 2>/dev/null || true\n\
\n\
# Check if running supervisor or direct command\n\
if [ "$1" = "supervisord" ] || [ "$1" = "/usr/bin/supervisord" ]; then\n\
    echo "Starting supervisord as root..."\n\
    exec "$@"\n\
else\n\
    echo "Running command as appuser: $@"\n\
    exec gosu appuser "$@"\n\
fi\n' > /entrypoint.sh && \
    chmod +x /entrypoint.sh

# Create supervisor configuration with better debugging
RUN cat > /etc/supervisor/conf.d/app.conf << 'EOF'
[supervisord]
nodaemon=true
user=root
logfile=/var/log/supervisor/supervisord.log
pidfile=/var/run/supervisor/supervisord.pid
childlogdir=/var/log/supervisor
loglevel=debug

[unix_http_server]
file=/var/run/supervisor/supervisor.sock
chmod=0700

[supervisorctl]
serverurl=unix:///var/run/supervisor/supervisor.sock

[rpcinterface:supervisor]
supervisor.rpcinterface_factory = supervisor.rpcinterface:make_main_rpcinterface

[program:main-app]
command=python -m uvicorn main:app --host 0.0.0.0 --port 8000 --workers 1 --log-level debug
directory=/app
user=appuser
autostart=true
autorestart=true
redirect_stderr=false
stdout_logfile=/var/log/supervisor/app.log
stderr_logfile=/var/log/supervisor/app_error.log
stdout_logfile_maxbytes=10MB
stdout_logfile_backups=3
stderr_logfile_maxbytes=10MB
stderr_logfile_backups=3
environment=DISPLAY=":99",PLAYWRIGHT_BROWSERS_PATH="/ms-playwright",PYTHONPATH="/app",PYTHONUNBUFFERED="1"
startretries=2
startsecs=10
stopwaitsecs=30

[program:webui]
command=python webui.py
directory=/app
user=appuser
autostart=true
autorestart=true
redirect_stderr=false
stdout_logfile=/var/log/supervisor/webui.log
stderr_logfile=/var/log/supervisor/webui_error.log
stdout_logfile_maxbytes=10MB
stdout_logfile_backups=3
stderr_logfile_maxbytes=10MB
stderr_logfile_backups=3
environment=DISPLAY=":99",PYTHONPATH="/app",PYTHONUNBUFFERED="1"
startretries=2
startsecs=10
stopwaitsecs=30
EOF

# Expose ports
EXPOSE 8000 7788

# Health check
HEALTHCHECK --interval=30s --timeout=15s --start-period=90s --retries=3 \
    CMD curl -f http://localhost:8000/health && curl -f http://localhost:7788/ || exit 1

# Set entrypoint and default command
ENTRYPOINT ["/entrypoint.sh"]
CMD ["supervisord", "-c", "/etc/supervisor/conf.d/app.conf"]