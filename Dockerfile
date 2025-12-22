FROM mcr.microsoft.com/playwright/python:v1.49.1-noble

# Set working directory
WORKDIR /app

# Copy requirements and install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install browsers
RUN playwright install chromium --with-deps

# Copy script files
COPY . .

# Create volume mounting points
RUN mkdir -p /app/downloads

# Metadata for persistence
VOLUME ["/app/downloads", "/app/.otter_state.json", "/app/.otter_session.json"]

# Run the downloader in quick mode by default
ENTRYPOINT ["python", "otter_downloader.py"]
CMD ["--quick"]
