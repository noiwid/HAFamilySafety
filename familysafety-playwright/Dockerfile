ARG BUILD_FROM=ghcr.io/hassio-addons/debian-base:7.3.3
FROM ${BUILD_FROM}

# Set shell
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# Install system dependencies required for Playwright and Chromium
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        python3 \
        python3-pip \
        python3-dev \
        curl \
        nodejs \
        npm \
        ca-certificates \
        fonts-liberation \
        libnss3 \
        libatk-bridge2.0-0 \
        libdrm2 \
        libxkbcommon0 \
        libxcomposite1 \
        libxdamage1 \
        libxfixes3 \
        libxrandr2 \
        libgbm1 \
        libasound2 \
        xvfb \
        x11vnc \
        novnc \
        python3-websockify \
        fluxbox \
        dbus \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Set environment variables for Playwright
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
ENV PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=0
ENV PYTHONUNBUFFERED=1

# Set working directory
WORKDIR /app

# Copy requirements and install Python dependencies
COPY requirements.txt .
RUN pip3 install --no-cache-dir --break-system-packages -r requirements.txt

# Install Playwright and its browsers
RUN pip3 install --no-cache-dir --break-system-packages playwright && \
    playwright install --with-deps chromium

# Copy application code
COPY app/ /app/app/
COPY rootfs/ /

# Set permissions
RUN chmod a+x /usr/local/bin/run.sh && \
    chmod a+x /etc/services.d/familysafety/run && \
    chmod a+x /etc/services.d/familysafety/finish

# Create necessary directories
RUN mkdir -p /share/familysafety && \
    chmod 700 /share/familysafety

# Expose ports
EXPOSE 8098 6081

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD curl -f http://localhost:8098/api/health || exit 1

# Labels
LABEL \
    io.hass.name="Microsoft Family Safety Auth" \
    io.hass.description="Authentication service for Microsoft Family Safety" \
    io.hass.type="addon" \
    io.hass.version="1.0.0"
