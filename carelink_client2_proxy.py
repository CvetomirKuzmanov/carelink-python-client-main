import carelink_client2
import argparse
import time
import json
import sys
import signal
import threading
import logging as log
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http import HTTPStatus
import os

VERSION = "1.2"

# Logging config
FORMAT = '[%(asctime)s:%(levelname)s] %(message)s'
log.basicConfig(format=FORMAT, datefmt='%Y-%m-%d %H:%M:%S', level=log.INFO)

# HTTP server settings
HOSTNAME = "0.0.0.0"
PORT = int(os.environ.get("PORT", 8081))
APIURL = "carelink"
OPT_NOHISTORY = "nohistory"

UPDATE_INTERVAL = 300
RETRY_INTERVAL = 120

# Token handling
CARELINK_TOKEN = os.environ.get("CARELINK_TOKEN")
wait_for_params = True

# Status messages
STATUS_INIT = "Initialization"
STATUS_DO_LOGIN = "Performing login"
STATUS_LOGIN_OK = "Login successful"
STATUS_NEED_TKN = "Valid token required"
g_status = STATUS_INIT

recentData = None
verbose = os.environ.get("VERBOSE", "false").lower() == "true"


def on_sigterm(signum, frame):
    log.debug("Exiting on signal")
    sys.exit()


def get_essential_data(data):
    mydata = ""
    if data is not None:
        mydata = data["patientData"].copy()
        for key in ["sgs", "markers", "limits", "notificationHistory"]:
            try:
                del mydata[key]
            except (KeyError, TypeError):
                pass
    return mydata


def webgui(status):
    head = '<!DOCTYPE html><html><head><title>Carelink Proxy</title></head><body>'
    body = f'<h2>Status: {status}</h2>'
    tail = f'<footer>Version {VERSION} | <a href="https://github.com/ondrej1024/carelink-python-client">carelink_client2_proxy</a></footer></body></html>'
    return head + body + tail


class MyServer(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # disable logging

    def do_GET(self):
        global recentData
        log.debug("Received GET request from %s" % self.address_string())

        if self.path.strip("/") == APIURL:
            sendData = json.loads(json.dumps(recentData))
            if sendData and "patientData" in sendData:
                for key in ["sgs", "meterData"]:
                    if key in sendData["patientData"]:
                        for entry in sendData["patientData"][key]:
                            if "value" in entry and isinstance(entry["value"], (int, float)):
                                entry["value"] = round(entry["value"] / 18, 1)  # mg/dL → mmol/L

            response = json.dumps(sendData)
            status_code = HTTPStatus.OK
            content_type = "application/json"

        elif self.path.strip("/") == f"{APIURL}/{OPT_NOHISTORY}":
            sendData = get_essential_data(recentData)
            if sendData and "sgs" in sendData:
                for entry in sendData["sgs"]:
                    if "value" in entry and isinstance(entry["value"], (int, float)):
                        entry["value"] = round(entry["value"] / 18, 1)
            response = json.dumps(sendData)
            status_code = HTTPStatus.OK
            content_type = "application/json"

        elif self.path == "/":
            response = webgui(status=g_status)
            status_code = HTTPStatus.OK
            content_type = "text/html"
        else:
            response = ""
            status_code = HTTPStatus.NOT_FOUND
            content_type = "text/html"

        self.send_response(status_code)
        self.send_header("Content-type", content_type)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try:
            self.wfile.write(bytes(response, "utf-8"))
        except BrokenPipeError:
            pass


def webserver_thread():
    webserver = ThreadingHTTPServer((HOSTNAME, PORT), MyServer)
    log.info("HTTP server started at http://%s:%s" % (HOSTNAME, PORT))
    webserver.serve_forever()


def start_webserver():
    t = threading.Thread(target=webserver_thread)
    t.daemon = True
    t.start()


# CLI arguments
parser = argparse.ArgumentParser()
parser.add_argument('--wait', '-w', type=int, help='Wait seconds between calls', required=False)
parser.add_argument('--verbose', '-v', help='Verbose mode', action='store_true')
args = parser.parse_args()

wait = UPDATE_INTERVAL if args.wait is None else args.wait
if args.verbose:
    verbose = True

if verbose:
    log.setLevel(log.DEBUG)

log.info("Starting Carelink Client Proxy (version %s)" % VERSION)
signal.signal(signal.SIGTERM, on_sigterm)
signal.signal(signal.SIGINT, on_sigterm)
start_webserver()

# Main process loop
while True:
    if CARELINK_TOKEN is None:
        log.error("CARELINK_TOKEN env variable not set")
        g_status = STATUS_NEED_TKN
        time.sleep(10)
        continue

    client = carelink_client2.CareLinkClient(token=CARELINK_TOKEN)
    g_status = STATUS_DO_LOGIN

    if client.init():
        g_status = STATUS_LOGIN_OK
        i = 0
        while True:
            i += 1
            log.debug("Starting download %d" % i)

            try:
                recentData = client.getRecentData()
                code = client.getLastResponseCode()
                if recentData and code == HTTPStatus.OK:
                    log.debug("New data received")
                elif code in [HTTPStatus.FORBIDDEN, HTTPStatus.UNAUTHORIZED]:
                    log.error("Authorization error (response %d)" % code)
                    break
                else:
                    log.error("Connection error (response %d)" % code)
                    time.sleep(60)
                    continue
            except Exception as e:
                log.error(e)
                recentData = None
                time.sleep(60)
                continue

            try:
                nextReading = int(recentData["lastConduitUpdateServerTime"]/1000) + wait
                tmoSeconds = int(nextReading - time.time())
                if tmoSeconds < 0:
                    tmoSeconds = RETRY_INTERVAL
            except KeyError:
                tmoSeconds = RETRY_INTERVAL

            log.debug("Waiting %d seconds before next download" % tmoSeconds)
            time.sleep(tmoSeconds+10)

    log.info(STATUS_NEED_TKN)
    g_status = STATUS_NEED_TKN
    time.sleep(10)
