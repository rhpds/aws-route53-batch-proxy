FROM registry.access.redhat.com/ubi9/python-311:latest

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ src/

EXPOSE 8443

CMD ["python3", "-m", "uvicorn", "src.app:app", "--host", "0.0.0.0", "--port", "8443"]
