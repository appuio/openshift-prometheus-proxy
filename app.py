#!/usr/bin/env python3

import os
import pathlib
import re
import sys
from http.server import HTTPServer
from socketserver import ForkingMixIn

import kubernetes
import openshift.dynamic
import openshift.dynamic.exceptions
import requests
from kubernetes.client.rest import ApiException
from prometheus_client import CollectorRegistry, MetricsHandler
from prometheus_client.parser import text_string_to_metric_families

PROXY_PATH_REGEX = re.compile('/(nodes|services)/((https):)?([^/:]+)(:([0-9]+))?/proxy/(.*)')

# Prometheus custom collector filtering metrics by namespace label
class ProxyCollector(object):
    def __init__(self, metric_families, namespaces):
        self.metric_families = metric_families
        self.namespaces = namespaces

    def collect(self):
        for family in self.metric_families:
            # Only keep samples where the namespace label exists and its value is one of the namespaces the request user has access to.
            family.samples[:] = [sample for sample in family.samples if sample.labels.get('namespace', None) in self.namespaces]
            yield family


# Use our own HTTPServer so we can use our own handler and get access to the HTTP headers.
class ForkingHTTPServer(ForkingMixIn, HTTPServer):
    pass


# Prometheus metrics handler proxying and filtering metrics from an upstream exporter
class ProxyMetricsHandler(MetricsHandler):

    @staticmethod
    def load_kubernetes_config():
        if 'KUBERNETES_PORT' in os.environ:  # App is running on a k8s cluster
            kubernetes.config.load_incluster_config()
        else:  # App is running on a developer workstation
            kubernetes.config.load_kube_config()

        return kubernetes.client.Configuration()

    @staticmethod
    def get_kubernetes_namespace():
        if 'KUBERNETES_PORT' in os.environ:  # App is running on a k8s cluster
            namespace = pathlib.Path('/var/run/secrets/kubernetes.io/serviceaccount/namespace').read_text()
        else:  # App is running on a developer workstation
            active_context = kubernetes.config.list_kube_config_contexts()[1]
            namespace = active_context['context']['namespace']

        return namespace

    @staticmethod
    def get_service_ca_cert():
        if 'KUBERNETES_PORT' in os.environ:  # App is running on a k8s cluster
            return '/var/run/secrets/kubernetes.io/serviceaccount/service-ca.crt'
        else:
            return False

    @staticmethod
    def new_openshift_client(k8s_config, token):
        k8s_config.api_key = {"authorization": f"Bearer {token}"}
        k8s_client = kubernetes.client.ApiClient(k8s_config)

        return openshift.dynamic.DynamicClient(k8s_client)

    def do_GET(self):
        k8s_config = self.load_kubernetes_config()
        service_account_token = k8s_config.api_key['authorization'].partition(' ')[2]

        try:
            dyn_client = self.new_openshift_client(k8s_config, service_account_token)
            match = PROXY_PATH_REGEX.match(self.path)
            if not match:
                self.send_error(404, "Not found\n")
                return

            if match.group(1) == 'nodes':
                node = dyn_client.resources.get(api_version='v1', kind='Node').get(name=match.group(4))
                host = next(addr.address for addr in node.status.addresses if addr.type == 'InternalIP')
                schema = 'https'
                port = node.status.daemonEndpoints.kubeletEndpoint.Port
                ca_cert = k8s_config.ssl_ca_cert if k8s_config.verify_ssl else False
            else:
                namespace = self.get_kubernetes_namespace()
                dyn_client.resources.get(api_version='v1', kind='Service').get(namespace=namespace, name=match.group(4))  # ensure the service exists
                host = f"{match.group(4)}.{namespace}.svc.cluster.local"
                schema = match.group(3) or 'http'
                port = match.group(6) or 8080
                ca_cert = self.get_service_ca_cert() if k8s_config.verify_ssl else False

            self.path = match.group(7)

            # Log into OpenShift cluster with the bearer token passed in the HTTP request
            bearer_token = self.headers.get('X-Forwarded-Access-Token')
            dyn_client = self.new_openshift_client(k8s_config, bearer_token)

            # Determine all namespaces the user identified by the bearer token has access to
            project_list = dyn_client.resources.get(api_version='project.openshift.io/v1', kind='Project').get()
            namespaces = {project.metadata.name for project in project_list.items}
        except ApiException as e:
            self.send_error(e.status, e.body, e.headers.get('Content-Type', 'text/plain'))
            return

        # Read metrics from upstream, using the pod service account
        r = requests.get(f"{schema}://{host}:{port}/{self.path}", headers={'authorization': f"Bearer {service_account_token}"}, verify=ca_cert)
        if r.status_code != requests.codes.ok:
            self.send_error(r.status_code, r.content)
            return

        # Parse upstream metrics and pass through ProxyCollector for filtering by namespace
        metric_families = text_string_to_metric_families(r.text)
        self.registry = CollectorRegistry()
        self.registry.register(ProxyCollector(metric_families, namespaces))

        # Export filtered metrics
        super().do_GET()

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
    httpd = ForkingHTTPServer(('127.0.0.1', 8080), ProxyMetricsHandler)
    httpd.serve_forever()
