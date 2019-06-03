# openshift-prometheus-proxy

openshift-prometheus-proxy is a filtering proxy for OpenShift Prometheus metric endpoints. It
is designed for use on shared OpenShift clusters to give customers access to kube-state-metrics and
kubelet metrics of their projects only.

openshift-prometheus-proxy can either proxy requests for the kubelet of each OpenShift node or for
pods running in the same namespace as openshift-prometheus-proxy, e.g. kube-state-metrics.
For security reasons no other endpoints may be accessed through the proxy.
openshift-prometheus-proxy requires authentication with a valid OpenShift bearer token. This
token also determines which metrics will be filtered by the proxy. Only metrics with a
`namespace` label whose value is a namespace the given bearer token has access to will be returned.
That is users can only access metrics concerning their namespaces.

## Requirements

* OpenShift Container Platform 3.9 or later
* OpenShift OAuth Proxy

OpenShift OAuth Proxy is used to restrict access to openshift-prometheus-proxy to select OpenShift users.

## Installation

Installation of openshift-prometheus-proxy and kube-state-metrics is based on OpenShift templates and parameter files.
Clone the repository and copy the openshift-prometheus-proxy example parameter file:

```sh
git clone https://github.com/appuio/openshift-prometheus-proxy
cd openshift-prometheus-proxy
cp env/openshift-prometheus-proxy.env.sample env/openshift-prometheus-proxy.env
```

Adapt `env/openshift-promtheus-proxy.env` to your setup:

* NAMESPACE: OpenShift Namespace to deploy openshift-prometheus-proxy to.
* OPENSHIFT_PROMETHEUS_PROXY_HOSTNAME: External hostname of openshift-prometheus-proxy.

### openshift-prometheus-proxy

openshift-prometheus-proxy can be installed with the following commands:

```sh
. env/openshift-prometheus-proxy.env
oc new-project ${NAMESPACE}
oc adm policy add-cluster-role-to-user -n ${NAMESPACE} --rolebinding-name=system:auth-delegator system:auth-delegator -z openshift-prometheus-proxy
oc adm policy add-cluster-role-to-user -n ${NAMESPACE} --rolebinding-name=system:node-reader system:node-reader -z openshift-prometheus-proxy
oc process -f template/openshift-prometheus-proxy.yaml --param-file=env/openshift-prometheus-proxy.env --ignore-unknown-parameters | oc apply -n ${NAMESPACE} -f -
```

The `system:auto-delegator` cluster role is needed by the OpenShift OAuth Proxy for bearer token authentication while the
`system:node-reader` cluster role is needed by openshift-prometheus-proxy to access the kubelet metrics of the OpenShift nodes.

By default customers aren't allowed to use openshift-prometheus-proxy. To give customers access the `access-openshift-prometheus-proxy`
role needs to be added to their Prometheus service account, e.g.:

```sh
. env/openshift-prometheus-proxy.env
PROMETHEUS_SA=<customer prometheus service account, e.g. system:serviceaccount:customer-prometheus:prometheus-apps>
oc policy add-role-to-user -n ${NAMESPACE} --role-namespace=${NAMESPACE} access-openshift-prometheus-proxy ${PROMETHEUS_SA} 
```

### kube-state-metrics

```sh
oc adm policy add-cluster-role-to-user -n ${NAMESPACE} --rolebinding-name=system:auth-delegator system:auth-delegator -z kube-state-metrics 
oc adm policy add-cluster-role-to-user -n ${NAMESPACE} --rolebinding-name=cluster-reader cluster-reader -z kube-state-metrics
oc process -f template/kube-state-metrics.yaml | oc apply -n ${NAMESPACE} -f -
```

Access to kube-state-metrics is protected by an OpenShift OAuth Proxy running in the same pod.
The `system:auto-delegator` cluster role is needed by the OpenShift OAuth Proxy for bearer token authentication while the
`cluster-reader` cluster role is needed by kube-state-metrics to collect metrics from OpenShift.

## Usage

openshift-prometheus-proxy supports the following URLs:

* `https://<openshift-prometheus-proxy hostname>/nodes/<node>/proxy/<path>`
* `https://<openshift-prometheus-proxy hostname>/services/[https:]<service>[:<port>]/proxy/<path>`

Where the values in angle brackets have the following meaning:

* openshift-prometheus-proxy hostname: external hostname of openshift-prometheus-proxy as configured in its route
* node: resource name of an OpenShift node to scrape for metrics, i.e. as shown by `oc get nodes`
* service: service to scrape for metrics. Can be prefix with `https:` to scrape with HTTPS, otherwise HTTP will be used.
* port: port to scrape for metrics, defaults to 8080.
* path: HTTP path to scrape for metrics

This repository contains templates with suitable Services and ServiceMonitors to scrape kubelet and kube-state-metrics
through openshift-prometheus-proxy. The Services are needed for the Prometheus service discovery.
The templates can be instantiated in the customers namespaces as follows.

### Prepare ServiceMonitor installation

Copy the sample parameter file for the templates:

```sh
cp env/monitor-openshift.env.sample env/monitor-openshift.env
```

Adapt `env/monitor-openshift.env` to your setup:

* NAMESPACE: Namespace of customer Prometheus instance.
* SERVICE_MONITOR_SKIP_TLS_VERIFY: Whether the ServiceMonitors should skip TLS verification. Not recommended on production. Defaults to 'false'.
* PROMETHEUS_ID: Value of the `prometheus` label of the ServiceMonitors. Must correspond to the `serviceMonitorSelector` in the customers `Prometheus` object. Defaults to 'app'.
* OPENSHIFT_PRMETHEUS_PROXY_HOSTNAME: External hostname of openshift-prometheus-proxy.

### Install kubelet ServiceMonitor

The `monitor-kubelet` template also deploys a DaemonSet with dummy Pods in order to discover the kubelet endpoints.

```sh
. env/monitor-openshift.env
oc process -f template/monitor-kubelet.yaml --param-file=env/monitor-openshift.env --ignore-unknown-parameters | oc apply -n ${NAMESPACE} -f -
```

### Install kube-state-metrics ServiceMonitor

```sh
. env/monitor-openshift.env
oc process -f template/monitor-kube-state-metrics.yaml --param-file=env/monitor-openshift.env --ignore-unknown-parameters | oc apply -n ${NAMESPACE} -f -
```
