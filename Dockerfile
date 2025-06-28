FROM python:3.9-slim

# Install system dependencies
RUN apt-get update && apt-get install -y \
    wget \
    gnupg \
    ca-certificates \
    procps \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements first for better caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright and browsers
RUN playwright install chromium
RUN playwright install-deps chromium

# Copy application code
COPY . .

# Copy templates and create necessary directories
COPY templates templates
RUN mkdir -p uploads results

# Expose port
EXPOSE 5000

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV PORT=5000

# Run the application
CMD gunicorn app:app --bind 0.0.0.0:$PORT