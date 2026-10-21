FROM python@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170
COPY worker.py /opt/rio/worker.py
USER 65532:65532
ENV PYTHONDONTWRITEBYTECODE=1 TMPDIR=/work HOME=/work
ENTRYPOINT ["/usr/local/bin/python", "-I", "-B", "/opt/rio/worker.py"]
