from __future__ import annotations

METRICS_PATH = "/metrics"
JOB_NAME = "sa-home-nodes"
JOB_VPN = "sa-home-vpn"
DATASOURCE_UID = "vm"
DATASOURCE_TYPE = "prometheus"
CONTENT_TYPE = "text/plain; version=0.0.4"

METRIC_UP = "sa_up"
HOST_METRIC_PREFIX = "sa_host_"
METRIC_TEMPERATURE = "sa_temperature_celsius"
METRIC_SCRAPE_UP = "up"

LABEL_NODE = "node"
LABEL_KIND = "kind"
LABEL_COMPONENT = "component"
LABEL_OBSERVER = "observer"
LABEL_SERVER = "server"
LABEL_TRANSPORT = "transport"
LABEL_TARGET = "target"
LABEL_SCOPE = "scope"

METRIC_VPN_CHECK_OK = "sa_vpn_check_ok"
METRIC_VPN_CHECK_LATENCY_MS = "sa_vpn_check_latency_ms"
METRIC_VPN_CHECK_OK_RATIO = "sa_vpn_check_ok_ratio"
METRIC_VPN_LINK_LEVEL = "sa_vpn_link_level"
