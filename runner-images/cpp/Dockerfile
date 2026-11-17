# Runner image for cpp17 submissions: g++ plus the trusted harness.
# Static linking keeps the produced binary independent of the image at run time.
FROM alpine:3.20

RUN apk add --no-cache g++ musl-dev python3 \
 && mkdir -p /box /opt/runner && chown 65534:65534 /box
COPY harness.py /opt/runner/harness.py
RUN chmod 0555 /opt/runner/harness.py

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 65534:65534
WORKDIR /box
ENTRYPOINT ["python3", "-u", "/opt/runner/harness.py"]
