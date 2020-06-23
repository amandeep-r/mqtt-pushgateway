FROM alpine:latest

RUN apk add --no-cache python3
RUN apk update
RUN apk upgrade
RUN apk add bash

COPY . /app
WORKDIR /app

RUN pip3 install --no-cache-dir -r /app/requirements.txt

EXPOSE 9466
# CMD [ "/usr/bin/python3" ]
CMD [ "/usr/bin/python3", "/app/mqtt_pushgateway.py" ]
