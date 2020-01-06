#!/usr/bin/env python3

import os
import re
import sys
from functools import partial
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

import kubernetes
import openshift.dynamic
import openshift.dynamic.exceptions
import requests
from kubernetes.client.rest import ApiException


class ProxyHTTPServer(ThreadingMixIn, HTTPServer):
    pass


class ProxyConfig():
    def __init__(self):
        self.upstream = os.getenv('UPSTREAM')
        if not self.upstream:
            print("Missing upstream Prometheus URL in environment variable 'UPSTREAM'!", file=sys.stderr)
            sys.exit(1)

        self.ssl_verify = os.getenv('SSL_VERIFY', 'true').lower()
        if self.ssl_verify == 'true':
            self.ssl_verify = True
        elif self.ssl_verify == 'false':
            self.ssl_verify = False
        elif self.ssl_verify == 'service':
            self.ssl_verify = '/var/run/secrets/kubernetes.io/serviceaccount/service-ca.crt'
        else:
            print("Invalid value '{self.ssl_verify}' in environment variable 'SSL_VERIFY'", file=sys.stderr)
            sys.exit(1)

        if 'KUBERNETES_PORT' in os.environ:  # App is running on a k8s cluster
            kubernetes.config.load_incluster_config()
        else:  # App is running on a developer workstation
            kubernetes.config.load_kube_config()

        self.k8s_config = kubernetes.client.Configuration()
        self.service_account_token = self.k8s_config.api_key['authorization'].partition(' ')[2]


# HTTP request handler proxying and filtering from a Prometheus federation endpoint
class ProxyMetricsHandler(BaseHTTPRequestHandler):
    PROXY_PATH_REGEX = re.compile('/([a-z0-9_-]*)')

    requests_session = requests.Session()

    def __init__(self, config, *args, **kwargs):
        self.config = config
        super().__init__(*args, **kwargs)

    @staticmethod
    def new_openshift_client(k8s_config, token):
        k8s_config.api_key = {"authorization": f"Bearer {token}"}
        k8s_client = kubernetes.client.ApiClient(k8s_config)

        return openshift.dynamic.DynamicClient(k8s_client)

    def do_GET(self):
        try:
            match = self.PROXY_PATH_REGEX.fullmatch(self.path)
            if not match:
                self.send_error(404, "Not found\n")
                return

            job = match.group(1)

            # Log into OpenShift cluster with the bearer token passed in the HTTP request
            bearer_token = self.headers.get('X-Forwarded-Access-Token')
            dyn_client = self.new_openshift_client(self.config.k8s_config, bearer_token)

            # Determine all namespaces the user identified by the bearer token has access to
            project_list = dyn_client.resources.get(api_version='project.openshift.io/v1', kind='Project').get()
            namespaces = {project.metadata.name for project in project_list.items}
        except ApiException as e:
            self.send_error(e.status, e.body, e.headers.get('Content-Type', 'text/plain'))
            return

        if not namespaces:
            self.send_error(403, f"Account '{self.headers.get('X-Forwarded-User', '<unknown>')}' doesn't have access to any namespaces!\n")
            return

        # Read metrics from upstream, using the pod service account
        namespaces = '|'.join(namespaces)
        r = self.requests_session.get(f"{self.config.upstream}/federate", params={'match[]': f'{{job="{job}",namespace=~"{namespaces}"}}'}, headers={'authorization': f"Bearer {self.config.service_account_token}"}, verify=self.config.ssl_verify)
        if r.status_code != requests.codes.ok:
            self.send_error(r.status_code, r.content)
            return

        self.send_response(200)
        self.send_header('Content-Type', self.headers.get('Content-Type', 'text/plain'))
        self.end_headers()
        for chunk in r.iter_content(chunk_size=4096):
            self.wfile.write(chunk)

    def send_error(self, status_code, message, content_type='text/plain'):
        self.send_response(status_code)
        self.send_header('Content-Type', content_type)
        self.end_headers()
        if isinstance(message, str):
            message = message.encode()
        self.wfile.write(message)

    def log_message(self, format, *args):
        sys.stderr.write("%s - %s [%s] %s\n" %
                         (self.headers.get('X-Forwarded-For', self.address_string()).partition(',')[0].strip(),
                          self.headers.get('X-Forwarded-User', '-'),
                          self.log_date_time_string(),
                          format%args))


if __name__ == '__main__':
    httpd = ProxyHTTPServer(('127.0.0.1', 8080), partial(ProxyMetricsHandler, ProxyConfig()))
    httpd.serve_forever()
