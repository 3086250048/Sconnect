ARG COTURN_IMAGE=coturn/coturn:4.18.0-r0-debian
FROM ${COTURN_IMAGE}

USER root

RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 \
 && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/opt/sconnect \
    HOME=/tmp \
    TMPDIR=/tmp

WORKDIR /opt/sconnect

COPY --chown=nobody:nogroup app /opt/sconnect/app
COPY --chown=nobody:nogroup scripts /opt/sconnect/scripts

RUN mkdir -p /data /certs \
 && chown -R nobody:nogroup /data /opt/sconnect \
 && chmod 0750 /data \
 && chmod 0755 /opt/sconnect/scripts/turnctl /opt/sconnect/scripts/test-turn

VOLUME ["/data"]

USER nobody:nogroup

EXPOSE 34780-34799/tcp 34780-34799/udp 50000-51279/udp 54000-54019/tcp 54000-54019/udp

STOPSIGNAL SIGTERM

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD python3 -c "import os, ssl, urllib.request; secure=bool(os.getenv('SCONNECT_API_TLS_CERT')); scheme='https' if secure else 'http'; context=ssl._create_unverified_context() if secure else None; urllib.request.urlopen(scheme + '://127.0.0.1:' + os.getenv('SCONNECT_PORT', '8080') + '/healthz', timeout=2, context=context).read()"

ENTRYPOINT ["python3", "-m", "app"]
CMD []
