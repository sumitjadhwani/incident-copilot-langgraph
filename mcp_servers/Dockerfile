# One image, three servers: pick with the command (python -m mcp_servers.<k8s_read|k8s_write|observability>).
FROM python:3.13-slim
RUN useradd --uid 10001 --create-home app
WORKDIR /srv
COPY mcp_servers/requirements.txt /srv/requirements.txt
RUN pip install --no-cache-dir -r /srv/requirements.txt
COPY mcp_servers /srv/mcp_servers
USER app
ENV PYTHONUNBUFFERED=1 PORT=8000
EXPOSE 8000
