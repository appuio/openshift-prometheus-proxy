#!/usr/bin/env python3

import json
import os
import re
import sys
import traceback
from functools import partial
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from urllib.parse import parse_qs, unquote, unquote_plus, urlencode, urlparse

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

        self.k8s_config = kubernetes.client.Configuration().get_default_copy()
        self.service_account_token = self.k8s_config.api_key['authorization'].partition(' ')[2]


# HTTP request handler proxying and filtering from a Prometheus federation endpoint
class ProxyMetricsHandler(BaseHTTPRequestHandler):
    INSTANCE_VECTOR_SELECTOR_REGEX = re.compile('([a-zA-Z_:][a-zA-Z0-9_:]*)?({.*})?')

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
            # Log into OpenShift cluster with the bearer token passed in the HTTP request
            bearer_token = self.headers.get('X-Forwarded-Access-Token')
            dyn_client = self.new_openshift_client(self.config.k8s_config, bearer_token)

            # Determine all namespaces the user identified by the bearer token has access to
            project_list = dyn_client.resources.get(api_version='project.openshift.io/v1', kind='Project').get()
            namespaces = {project.metadata.name for project in project_list.items}
        except ApiException as e:
            traceback.print_exc()
            self.send_error(e.status, e.body, e.headers.get('Content-Type', 'text/plain'))
            return

        if not namespaces:
            self.send_error(403, f"Account '{self.headers.get('X-Forwarded-User', '<unknown>')}' doesn't have access to any namespaces!\n")
            return

        namespace_selector = f"namespace=~\"{'|'.join(namespaces)}\""

        url = urlparse(self.path)

        if url.path == "/federate":
            self.get_federate(url, namespace_selector)
        elif url.path == "/jobs":
            self.get_jobs(url, namespace_selector)
        else:
            print(url.path)
            self.send_error(404, "Not found")
            return

    def get_federate(self, url, namespace_selector):
        query_args = parse_qs(url.query)
        match_args = query_args.get('match[]')
        if not match_args:
            self.send_error(400, "Missing match[] parameter\n")
            return
        for i, match_arg in enumerate(match_args):
            re_match = self.INSTANCE_VECTOR_SELECTOR_REGEX.fullmatch(match_arg)
            if not re_match:
                self.send_error(400, f"Not a valid vector selector: '{match_arg}'!")
                continue
                #return
            metric_name = re_match.group(1) or ''
            label_selectors = re_match.group(2)
            if label_selectors and label_selectors != '{}':
                match_args[i] = f"{metric_name}{label_selectors[:-1]},{namespace_selector}}}"
            else:
                match_args[i] = f"{metric_name}{{{namespace_selector}}}"

        # Read metrics from upstream, using the pod service account
        # namespaces = '|'.join(namespaces)
        r = self.requests_session.get(f"{self.config.upstream}/federate", params=query_args, headers={'authorization': f"Bearer {self.config.service_account_token}"}, verify=self.config.ssl_verify)
        if r.status_code != requests.codes.ok:
            self.send_error(r.status_code, r.content)
            return

        self.send_response(200)
        self.send_header('Content-Type', self.headers.get('Content-Type', 'text/plain'))
        self.end_headers()
        for chunk in r.iter_content(chunk_size=4096):
            self.wfile.write(chunk)

    def get_jobs(self, url, namespace_selector):
        query_args = {'query': f"count({{{namespace_selector}}}) by (job)"}
        r = self.requests_session.get(f"{self.config.upstream}/api/v1/query", params=query_args, headers={'authorization': f"Bearer {self.config.service_account_token}"}, verify=self.config.ssl_verify)
        if r.status_code != requests.codes.ok:
            self.send_error(r.status_code, r.content)
            return

        json_result = json.loads(r.content)
        jobs = [f"'{job['metric'].get('job', '')}'\n" for job in json_result['data']['result']]
        jobs.sort()

        self.send_response(200)
        self.send_header('Content-Type', self.headers.get('Content-Type', 'text/plain'))
        self.end_headers()
        self.wfile.write(''.join(jobs).encode())

    def send_error(self, status_code, message, content_type='text/plain'):
        self.send_response(status_code)
        self.send_header('Content-Type', content_type)
        self.end_headers()
        if isinstance(message, str):
            message = message.encode()
        self.wfile.write(message)

    def log_message(self, format, *args):
        if args:
            args = (unquote(args[0]),) + args[1:]
        sys.stderr.write("%s - %s [%s] %s\n" %
                         (self.headers.get('X-Forwarded-For', self.address_string()).partition(',')[0].strip(),
                          self.headers.get('X-Forwarded-User', '-'),
                          self.log_date_time_string(),
                          format%args))


if __name__ == '__main__':
    httpd = ProxyHTTPServer(('127.0.0.1', 8080), partial(ProxyMetricsHandler, ProxyConfig()))
    httpd.serve_forever()
