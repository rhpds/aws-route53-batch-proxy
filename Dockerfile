FROM registry.access.redhat.com/ubi9/python-312:latest AS builder

USER 0
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ /opt/app-root/src/src/

USER 1001

FROM builder AS verify
COPY tests/ /opt/app-root/src/tests/
RUN python -m pytest tests/ -v --tb=short

FROM builder AS runtime
WORKDIR /opt/app-root/src
ENV PYTHONPATH=/opt/app-root/src
ENV PORT=8443
ENV WORKERS=4
EXPOSE 8443

CMD ["sh", "-c", "uvicorn src.app:app --host 0.0.0.0 --port ${PORT} --workers ${WORKERS}"]
