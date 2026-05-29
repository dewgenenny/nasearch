FROM debian:bookworm-slim

RUN apt-get update -qq && \
    apt-get install -y -q --no-install-recommends \
      gosu \
      plocate \
      python3 \
      python3-pip \
      python3-venv && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN python3 -m venv /venv
ENV PATH="/venv/bin:$PATH"

COPY requirements.txt /app/requirements.txt
RUN pip install --quiet -r /app/requirements.txt

COPY app/ /app/

EXPOSE 8000

RUN useradd -r -u 1000 -g users -d /app -s /sbin/nologin nasearch && \
    chown -R nasearch:users /app

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
