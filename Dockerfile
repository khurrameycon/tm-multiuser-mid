# Multi-stage build for smaller, more secure image
FROM python:3.11-slim as builder

# Install build dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    make \
    && rm -rf /var/lib/apt/lists/*

# Create virtual environment
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy requirements and install Python dependencies
COPY requirements.txt /tmp/
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r /tmp/requirements.txt

# Production stage
FROM python:3.11-slim

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DEBIAN_FRONTEND=noninteractive \
    PATH="/opt/venv/bin:$PATH" \
    DOCKER_CONTAINER=1

# Install system dependencies for Playwright
RUN apt-get update && apt-get install -y \
    # Essential utilities
    curl \
    wget \
    gnupg \
    ca-certificates \
    # Playwright browser dependencies
    libnss3 \
    libnspr4 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdbus-1-3 \
    libdrm2 \
    libxkbcommon0 \
    libatspi2.0-0 \
    libx11-6 \
    libxcomposite1 \
    libxdamage1 \
    libxext6 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libxss1 \
    libasound2 \
    libpangocairo-1.0-0 \
    libatk1.0-0 \
    libcairo-gobject2 \
    libgtk-3-0 \
    libgdk-pixbuf2.0-0 \
    # For headless browser operation
    xvfb \
    # Fonts
    fonts-liberation \
    fonts-noto-color-emoji \
    fonts-noto-cjk \
    # Process management
    supervisor \
    # Cleanup
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# Copy virtual environment from builder
COPY --from=builder /opt/venv /opt/venv

# Create app user for security
RUN useradd -m -u 1000 -s /bin/bash appuser

# Set working directory
WORKDIR /app

# Copy application code
COPY --chown=appuser:appuser . .

# Create necessary directories with proper permissions
RUN mkdir -p /app/tmp/{logs,sessions,downloads,recordings,traces} \
    && mkdir -p /app/.cache/ms-playwright \
    && chown -R appuser:appuser /app

# Switch to app user
USER appuser

# Install Playwright browsers
RUN playwright install chromium && \
    playwright install-deps chromium || true

# Create entrypoint script
USER root
RUN echo '#!/bin/bash\n\
set -e\n\
\n\
# Start Xvfb for headless browser operation\n\
export DISPLAY=:99\n\
Xvfb :99 -screen 0 1024x768x24 > /dev/null 2>&1 &\n\
\n\
# Wait for Xvfb to start\n\
sleep 2\n\
\n\
# Switch to app user and start the application\n\
exec su-exec appuser "$@"\n\
' > /entrypoint.sh && chmod +x /entrypoint.sh

# Install su-exec for proper user switching
RUN apt-get update && apt-get install -y su-exec && rm -rf /var/lib/apt/lists/*

# Switch back to app user
USER appuser

# Expose port
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# Set entrypoint
ENTRYPOINT ["/entrypoint.sh"]

# Default command
CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]