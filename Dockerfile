FROM python:3.11-slim

WORKDIR /app

# Install system deps for any stdio backends (e.g., node/npx for puppeteer)
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        curl \
        ca-certificates && \
    rm -rf /var/lib/apt/lists/*

# Optional: install Node.js for stdio backends that need npx
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y nodejs && \
    rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY src/ ./src/
COPY static/ ./static/
COPY config.yaml .

EXPOSE 8080

ENTRYPOINT ["python", "-m", "src"]
