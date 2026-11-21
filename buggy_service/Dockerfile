FROM python:3.13-slim
RUN useradd --uid 10001 --create-home app
COPY app.py /app/app.py
USER app
ENV PYTHONUNBUFFERED=1
EXPOSE 8080
CMD ["python", "/app/app.py"]
