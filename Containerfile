FROM registry.access.redhat.com/ubi9/python-311:latest

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ src/

EXPOSE 8443 9090

CMD ["python3", "-m", "src.server"]
