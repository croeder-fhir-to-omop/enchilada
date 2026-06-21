FROM python:3.11-slim
WORKDIR /app
COPY pyproject.toml .
COPY enchilada/ enchilada/
RUN pip install --no-cache-dir .
COPY config.docker.yaml /app/config.docker.yaml
COPY certs/ /certs/
ENV ENCHILADA_CONFIG=/app/config.docker.yaml
EXPOSE 8081
CMD ["python", "-m", "enchilada"]
