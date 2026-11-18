# SANDBOX-VAL image: platform code and fixtures arrive as a copy; the host
# filesystem is never mounted.  Base image is pinned to python:3.12-slim.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Unprivileged runtime user.  /work is the only place that gets a writable
# tmpfs at run time; everything else stays on the read-only container root.
RUN groupadd --gid 10001 sandbox \
 && useradd --uid 10001 --gid 10001 --create-home --shell /bin/sh sandbox \
 && mkdir -p /work/platform \
 && chmod 0555 /work

WORKDIR /opt/platform
COPY .build/context/ /opt/platform/
COPY c_arm_once.py /opt/platform/sandbox/c_arm_once.py
COPY entrypoint.sh /usr/local/bin/sandbox-entrypoint

# Install exactly the platform requirements.  Then make the baked-in copy
# read-only for the runtime user and record its baseline SHA-256 manifest
# outside the copied tree (so it is not part of /work/platform).
RUN python -m pip install --no-cache-dir -r /opt/platform/requirements.txt \
 && find /opt/platform -type d -exec chmod 0755 {} + \
 && find /opt/platform -type f -exec chmod 0444 {} + \
 && chmod 0555 /usr/local/bin/sandbox-entrypoint \
 && cd /opt/platform \
 && find . -type f -print0 | LC_ALL=C sort -z | xargs -0 sha256sum > /opt/platform.sha256 \
 && chmod 0444 /opt/platform.sha256

USER sandbox
WORKDIR /work/platform
ENTRYPOINT ["/usr/local/bin/sandbox-entrypoint"]
CMD ["chain"]
