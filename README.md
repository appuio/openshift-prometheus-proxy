# openshift-prometheus-proxy

openshift-prometheus-proxy is a filtering proxy for Prometheus on OpenShift. It
is designed for use on shared OpenShift clusters to give customers access to kube-state-metrics and
kubelet metrics of their projects only.

openshift-prometheus-proxy acts a proxy for the federation endpoint of an Prometheus installation,
usually the Prometheus installation that comes with the OpenShift Container Platform. 
It requires authentication with a valid OpenShift bearer token. This
token also determines which metrics will be filtered by the proxy. Only metrics with a
`namespace` label whose value is a namespace the given bearer token has access to will be returned.
That is users can only access metrics concerning their namespaces.

## Requirements

* OpenShift Container Platform 3.11 or later
* OpenShift OAuth Proxy

OpenShift OAuth Proxy is used to restrict access to openshift-prometheus-proxy to select OpenShift users.

## Installation

Installation of openshift-prometheus-proxy is based on OpenShift templates and parameter files.
Clone the repository and copy the openshift-prometheus-proxy example parameter file:

```sh
git clone https://github.com/appuio/openshift-prometheus-proxy
cd openshift-prometheus-proxy
cp env/openshift-prometheus-proxy.env.sample env/openshift-prometheus-proxy.env
```

Adapt `env/openshift-promtheus-proxy.env` to your setup:

* NAMESPACE: OpenShift Namespace to deploy openshift-prometheus-proxy to.
* OPENSHIFT_PROMETHEUS_PROXY_HOSTNAME: External hostname of openshift-prometheus-proxy.
* OPENSHIFT_PROMETHEUS_PROXY_UPSTREAM: URL of the upstream Prometheus server.
* OPENSHIFT_PROMETHEUS_PROXY_TLS_VERIFY: How to verify the upstream SSL/TLS certificate. Can either be 'true' to verify against the root certificate bundle, 'service' to verify against the OpenShift service CA, or 'false' to don't verify at all (not recommended in production).

### openshift-prometheus-proxy

openshift-prometheus-proxy can be installed with the following commands:

```sh
. env/openshift-prometheus-proxy.env
oc new-project ${NAMESPACE}
oc adm policy add-cluster-role-to-user -n ${NAMESPACE} --rolebinding-name=system:auth-delegator system:auth-delegator -z openshift-prometheus-proxy
oc adm policy add-cluster-role-to-user -n ${NAMESPACE} --rolebinding-name=cluster-monitoring-view cluster-monitoring-view -z openshift-prometheus-proxy
oc process -f template/openshift-prometheus-proxy.yaml --param-file=env/openshift-prometheus-proxy.env --ignore-unknown-parameters | oc apply -n ${NAMESPACE} -f -
```

The `system:auto-delegator` cluster role is needed by the OpenShift OAuth Proxy for bearer token authentication and the
`cluster-monitoring-view` role is needed for read access to the OpenShift Prometheus instance.

By default customers aren't allowed to use openshift-prometheus-proxy. To give customers access the `access-openshift-prometheus-proxy`
role needs to be added to their Prometheus service account, e.g.:

```sh
. env/openshift-prometheus-proxy.env
PROMETHEUS_NAMESPACE=<customer prometheus namespace>
PROMETHEUS_SA=<customer prometheus service account, e.g. prometheus-apps>
oc policy add-role-to-user -n ${NAMESPACE} --role-namespace=${NAMESPACE} access-openshift-prometheus-proxy system:serviceaccount:${PROMETHEUS_NAMESPACE}:${PROMETHEUS_SA} 
```

## Usage

openshift-prometheus-proxy supports URLs of the same form as the 
[Prometheus federation endpoint](https://prometheus.io/docs/prometheus/latest/federation/),
i.e. `https://<openshift-prometheus-proxy hostname>/federate?match[]=<selector>`.
Where `<selector` is any [Prometheus instant vector selector](https://prometheus.io/docs/prometheus/latest/querying/basics/#instant-vector-selectors),
e.g. `{job="kubelet"}`. Additionally you can use `match[]={}` to select all metrics concerning your namespaces.

You can use curl to retrieve a list of available jobs: 
`curl -kH "Authorization: Bearer $(oc sa get-token -n $PROMETHEUS_NAMESPACE $PROMETHEUS_SA)" https://${OPENSHIFT_PROMETHEUS_PROXY_HOSTNAME}/jobs`.
Note that some metrics have an empty job label, i.e. `job=""`, e.g. metrics created by recording rules.

This repository contains a template with an example ServiceMonitor to scrape kubelet and kube-state-metrics
through openshift-prometheus-proxy. The template can be instantiated in the customers namespaces as follows.

Copy the sample parameter file for the templates:

```sh
cp env/monitor-openshift.env.sample env/monitor-openshift.env
```

Adapt `env/monitor-openshift.env` to your setup:

* NAMESPACE: Namespace of customer Prometheus instance.
* PROMETHEUS_ID: Value of the `prometheus` label of the ServiceMonitor. Must correspond to the `serviceMonitorSelector` in the customers `Prometheus` object. Defaults to 'app'.
* OPENSHIFT_PROMETHEUS_PROXY_SCRAPE_ENDPOINT: Endpoint of the OpenShift Prometheus Proxy to scrape, either the service or the route of the proxy.
* OPENSHIFT_PROMETHEUS_PROXY_SCRAPE_SKIP_TLS_VERIFY: Whether to skip TLS certificate verification when scraping OpenShift Prometheus Proxy. Not recommended on production. Defaults to 'false'.

Instantiate the openshift-prometheus-proxy scrape config template in the customer Prometheus namespace:

```sh
. env/monitor-openshift.env
oc process -f template/monitor-openshift-prometheus-proxy.yaml --param-file=env/monitor-openshift.env --ignore-unknown-parameters | oc apply -n ${NAMESPACE} -f -
```
