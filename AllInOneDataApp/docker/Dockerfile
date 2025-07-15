# Multi-stage Dockerfile for AllInOneDataApp
# Stage 1: Builder - install dependencies
FROM python:3.10-slim AS builder
WORKDIR /app
COPY requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt -t /app/packages

# Stage 2: Runtime - slim base image
FROM python:3.10-slim
# Create a non-root user for security
RUN useradd -m appuser
WORKDIR /app
# Copy installed packages from builder stage
COPY --from=builder /app/packages /app/packages
ENV PYTHONPATH="/app/packages:$PYTHONPATH"
# Copy application code
COPY app/ app/
# Expose Streamlit default port
EXPOSE 8501
# Switch to non-root user
USER appuser
# Entry point: run Streamlit app
CMD ["streamlit", "run", "app/app.py", "--server.address=0.0.0.0"]
