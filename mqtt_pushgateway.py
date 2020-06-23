#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# kate: space-indent on; indent-width 4; replace-tabs on;

import re
import pytoml
import logging
import socket
import time
import json
import ssl

import paho.mqtt.client as mqtt

from collections import defaultdict
from datetime    import datetime, timedelta

from flask import Flask, Response, redirect

app = Flask("mqtt_pushgateway")

with open("config.toml") as fd:
    config = pytoml.load(fd)


class Topic(object):
    def __init__(self):
        self.metric      = None
        self.keywords    = {}
        self.value       = None
        self.last_update = datetime.fromtimestamp(0)
        self.expire      = config["mqtt"].get("expire")
        self.ignore      = False
        self.known_vals  = set([])
        self.is_numeric  = True

    def update(self, topic, value):
        # topic is e.g. sensors/somewhere/temperature
        # we want to apply our regexen, see if one matches
        # if one matches, use it to determine self.metric and self.keywords
        if self.metric is None:
            for cfg_topic in config["topic"]:
                if "match" in cfg_topic:
                    m = re.match(cfg_topic["match"], topic)
                    if m is not None:
                        self.keywords = m.groupdict()
                        self.expire = cfg_topic.get("expire")
                        self.ignore = cfg_topic.get("ignore", False)
                        if "__metric__" in self.keywords:
                            self.metric = self.keywords.pop("__metric__")
                        if "metric" in cfg_topic:
                            self.metric = cfg_topic["metric"]
                        break

            if self.metric is None:
                self.metric = topic.rsplit("/", 1)[1]

            if self.expire is None:
                self.expire = config["mqtt"].get("expire")

            self.keywords["mqtt_topic"] = topic

        try:
            self.value = float(value)
            self.is_numeric = True
        except (TypeError, ValueError):
            self.value = value
            self.known_vals.add(self.value)
            self.is_numeric = False

        self.last_update = datetime.now()

    @property
    def forget(self):
        return datetime.now() - self.last_update > timedelta(hours=1)

    def __str__(self):
        data_age = (datetime.now() - self.last_update).total_seconds()

        if self.is_numeric:
            if self.expire is not None and data_age > self.expire:
                # metric is expired, return data age only
                template =  'mqtt_data_age{%(kwds)s,metric="%(metric)s"} %(age)f'
            else:
                template = ('%(metric)s{%(kwds)s} %(value)f\n'
                            'mqtt_data_age{%(kwds)s,metric="%(metric)s"} %(age)f')

            return template % dict(
                metric = self.metric,
                kwds   = ','.join([ '%s="%s"' % item for item in self.keywords.items() ]),
                value  = self.value,
                age    = data_age
            )

        else:
            series = ['mqtt_data_age{%(kwds)s,metric="%(metric)s"} %(age)f' % dict(
                metric = self.metric,
                kwds   = ','.join([ '%s="%s"' % item for item in self.keywords.items() ]),
                age    = data_age
            )]
            if self.expire is None or data_age < self.expire:
                for known_val in self.known_vals:
                    # generate one time series for each known value, where the value is 1
                    # for the current value and 0 for all else
                    series.append('%(metric)s{%(kwds)s} %(value)f' % dict(
                        metric = self.metric,
                        kwds   = ','.join([ '%s="%s"' % item for item in dict(self.keywords, **{self.metric: known_val}).items() ]),
                        value  = int(known_val == self.value)
                    ))
            return "\n".join(series)


metrics = defaultdict(Topic)


@app.route("/")
def http_index():
    return redirect("/metrics", code=302)

@app.route("/metrics")
def http_metrics():
    content = [str(metric)
        for metric in metrics.values()
        if not metric.ignore and not metric.forget
    ]
    return Response('\n'.join(content + ['']), mimetype="text/plain")

def babylon_farmstatus_hack(message):
    json_farmstatus = json.loads(message.payload.decode("utf-8"))
    topic = message.topic
    revised_topic = topic + "/" +str(json_farmstatus['serial_num'])
    return revised_topic    

def on_message(client, userdata, message):
    topic = message.topic
    if topic == "babylon/farm_out":
        topic = babylon_farmstatus_hack(message)

    try:
        payload = message.payload.decode("utf-8")
    except:
        logging.warning("Payload for '%s' is not valid utf-8, ignored" % topic, exc_info=True)
    else:
        payload = payload.strip()
        logging.info("Message received: %s => %s", topic, payload)

    if payload[0] == "{" and payload[-1] == "}":
        try:
            json_message = json.loads(payload)
        except ValueError:
            # payload is not json, do a standard update
            pass
        else:
            for key, val in json_message.items():
                key_topic = "{}/{}".format(topic, key)
                metrics[key_topic].update(key_topic, val)
            return

    try:
        metrics[topic].update(topic, payload)
    except:
        logging.warning("Metric update for '%s' failed" % topic, exc_info=True)

def main():
    # If you put a %(hostname) in the config file under client_id, the hostname will be included
    client = mqtt.Client(config["mqtt"]["client_id"] % dict(
        hostname=socket.gethostname()
    ))
    # Configure network encryption and authentication options. Enables SSL/TLS support.
    # We need this for AWS IOT
    client.tls_set(
        ca_certs = config["mqtt"]["rootCA_path"],
        certfile = config["mqtt"]["cert_path"],
        keyfile = config["mqtt"]["private_key_path"],
        cert_reqs = ssl.CERT_REQUIRED,
        # defaults to the highest version available
        tls_version = ssl.PROTOCOL_SSLv23,
    )
    # client.username_pw_set(config["mqtt"]["username"], config["mqtt"]["password"])
    client.on_message = on_message

    def on_connect(client, userdata, flags, result):
        logging.info("subscribing")
        for topic in config["mqtt"]["subscribe"]:
            client.subscribe(topic)

    client.on_connect = on_connect


    client.connect(config["mqtt"]["broker"], port=config["mqtt"]["port"])

    client.loop_start()
    client.enable_logger()


    app.debug = True

    try:
        app.run(host=config["exporter"]["listen"], port=config["exporter"]["port"])
    except KeyboardInterrupt:
        print("exiting")

    client.disconnect()
    client.loop_stop()


if __name__ == '__main__':
    main()
