FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends tzdata ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY server.py ./
COPY static ./static
RUN mkdir -p data
ENV HOST=0.0.0.0 PORT=8080 REPORTS=off PYTHONUNBUFFERED=1
EXPOSE 8080
CMD ["python", "server.py"]
