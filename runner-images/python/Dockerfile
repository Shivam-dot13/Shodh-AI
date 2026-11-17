# Runner image for python3 submissions.
# Contains an interpreter, the trusted harness, and nothing else. No shell tools are needed by
# the harness, and the container it runs in has no network and a read-only root filesystem.
FROM python:3.12-alpine

RUN mkdir -p /box /opt/runner && chown 65534:65534 /box
COPY harness.py /opt/runner/harness.py
RUN chmod 0555 /opt/runner/harness.py

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPYCACHEPREFIX=/box/pycache
USER 65534:65534
WORKDIR /box
ENTRYPOINT ["python3", "-u", "/opt/runner/harness.py"]
