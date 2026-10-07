FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    TELELOOM_DATA_DIR=/data HOME=/home/teleloom
COPY dist/*.whl /tmp/wheels/
RUN python -m pip install --no-cache-dir /tmp/wheels/*.whl \
    && rm -rf /tmp/wheels \
    && useradd --uid 10001 --create-home teleloom \
    && mkdir -p /data /home/teleloom/.local/share/teleloom/session-locks \
    && chmod 700 /data /home/teleloom /home/teleloom/.local/share/teleloom/session-locks \
    && chown -R teleloom:teleloom /data /home/teleloom
COPY scripts/container_entry.py /opt/teleloom-container.py
USER teleloom
WORKDIR /data
ENTRYPOINT ["python", "/opt/teleloom-container.py"]
CMD ["mcp"]
