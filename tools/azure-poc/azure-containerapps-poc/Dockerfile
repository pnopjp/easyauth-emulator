FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir h2

COPY __init__.py _sample_app_shared.py sample_app.py ./

ENV SAMPLE_APP_PORT=8081
EXPOSE 8081

CMD ["python", "sample_app.py"]
