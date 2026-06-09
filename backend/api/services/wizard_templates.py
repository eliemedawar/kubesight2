"""Built-in application templates for the Application Builder wizard."""

from __future__ import annotations

from typing import Any, Dict, List

TEMPLATES: List[Dict[str, Any]] = [
    {
        "id": "nginx",
        "name": "NGINX",
        "description": "High-performance web server and reverse proxy",
        "category": "Web",
        "workloadType": "Deployment",
        "containers": [{"name": "nginx", "image": "nginx", "tag": "latest", "pullPolicy": "IfNotPresent", "ports": [80]}],
        "resources": {"cpuRequest": "100m", "cpuLimit": "500m", "memoryRequest": "128Mi", "memoryLimit": "256Mi"},
        "networking": {"service": {"enabled": True, "type": "ClusterIP", "port": 80, "targetPort": 80, "protocol": "TCP"}},
        "scaling": {"replicas": 1},
        "healthChecks": {
            "readiness": {"enabled": True, "type": "http", "path": "/", "port": 80},
            "liveness": {"enabled": True, "type": "http", "path": "/", "port": 80},
        },
        "environment": {
            "envVars": [
                {"name": "BASE_URL", "value": ""},
                {"name": "SERVER_NAME", "value": ""},
                {"name": "LISTEN_PORT", "value": ""},
            ],
            "configMapRefs": [{"name": "nginx-config", "keys": []}],
            "secretRefs": [{"name": "nginx-secrets", "keys": []}],
        },
    },
    {
        "id": "apache",
        "name": "Apache HTTP Server",
        "description": "Popular open-source HTTP server",
        "category": "Web",
        "workloadType": "Deployment",
        "containers": [{"name": "apache", "image": "httpd", "tag": "2.4", "pullPolicy": "IfNotPresent", "ports": [80]}],
        "resources": {"cpuRequest": "100m", "cpuLimit": "500m", "memoryRequest": "128Mi", "memoryLimit": "256Mi"},
        "networking": {"service": {"enabled": True, "type": "ClusterIP", "port": 80, "targetPort": 80}},
        "scaling": {"replicas": 1},
        "environment": {
            "envVars": [
                {"name": "BASE_URL", "value": ""},
                {"name": "SERVER_NAME", "value": ""},
                {"name": "APACHE_PORT", "value": ""},
            ],
            "configMapRefs": [{"name": "apache-config", "keys": []}],
            "secretRefs": [{"name": "apache-secrets", "keys": []}],
        },
    },
    {
        "id": "redis",
        "name": "Redis",
        "description": "In-memory data structure store",
        "category": "Database",
        "workloadType": "StatefulSet",
        "containers": [{"name": "redis", "image": "redis", "tag": "7", "pullPolicy": "IfNotPresent", "ports": [6379]}],
        "resources": {"cpuRequest": "100m", "cpuLimit": "500m", "memoryRequest": "256Mi", "memoryLimit": "512Mi"},
        "networking": {"service": {"enabled": True, "type": "ClusterIP", "port": 6379, "targetPort": 6379}},
        "scaling": {"replicas": 1},
        "storage": {"pvcMode": "new", "newPvc": {"name": "redis-data", "size": "1Gi", "accessMode": "ReadWriteOnce"}},
        "environment": {
            "envVars": [
                {"name": "REDIS_PASSWORD", "value": ""},
                {"name": "REDIS_MAXMEMORY", "value": ""},
            ],
            "configMapRefs": [{"name": "redis-config", "keys": []}],
            "secretRefs": [{"name": "redis-secrets", "keys": []}],
        },
    },
    {
        "id": "postgres",
        "name": "PostgreSQL",
        "description": "Advanced open-source relational database",
        "category": "Database",
        "workloadType": "StatefulSet",
        "containers": [{"name": "postgres", "image": "postgres", "tag": "16", "pullPolicy": "IfNotPresent", "ports": [5432]}],
        "environment": {
            "envVars": [
                {"name": "POSTGRES_USER", "value": ""},
                {"name": "POSTGRES_PASSWORD", "value": "changeme"},
                {"name": "POSTGRES_DB", "value": "app"},
            ],
            "configMapRefs": [{"name": "postgres-config", "keys": []}],
            "secretRefs": [{"name": "postgres-secrets", "keys": []}],
        },
        "resources": {"cpuRequest": "250m", "cpuLimit": "1000m", "memoryRequest": "512Mi", "memoryLimit": "1Gi"},
        "networking": {"service": {"enabled": True, "type": "ClusterIP", "port": 5432, "targetPort": 5432}},
        "scaling": {"replicas": 1},
        "storage": {"pvcMode": "new", "newPvc": {"name": "postgres-data", "size": "5Gi", "accessMode": "ReadWriteOnce"}},
    },
    {
        "id": "mysql",
        "name": "MySQL",
        "description": "Popular open-source relational database",
        "category": "Database",
        "workloadType": "StatefulSet",
        "containers": [{"name": "mysql", "image": "mysql", "tag": "8", "pullPolicy": "IfNotPresent", "ports": [3306]}],
        "environment": {
            "envVars": [
                {"name": "MYSQL_ROOT_PASSWORD", "value": "changeme"},
                {"name": "MYSQL_DATABASE", "value": ""},
                {"name": "MYSQL_USER", "value": ""},
            ],
            "configMapRefs": [{"name": "mysql-config", "keys": []}],
            "secretRefs": [{"name": "mysql-secrets", "keys": []}],
        },
        "resources": {"cpuRequest": "250m", "cpuLimit": "1000m", "memoryRequest": "512Mi", "memoryLimit": "1Gi"},
        "networking": {"service": {"enabled": True, "type": "ClusterIP", "port": 3306, "targetPort": 3306}},
        "scaling": {"replicas": 1},
        "storage": {"pvcMode": "new", "newPvc": {"name": "mysql-data", "size": "5Gi", "accessMode": "ReadWriteOnce"}},
    },
    {
        "id": "mongodb",
        "name": "MongoDB",
        "description": "Document-oriented NoSQL database",
        "category": "Database",
        "workloadType": "StatefulSet",
        "containers": [{"name": "mongo", "image": "mongo", "tag": "7", "pullPolicy": "IfNotPresent", "ports": [27017]}],
        "resources": {"cpuRequest": "250m", "cpuLimit": "1000m", "memoryRequest": "512Mi", "memoryLimit": "1Gi"},
        "networking": {"service": {"enabled": True, "type": "ClusterIP", "port": 27017, "targetPort": 27017}},
        "scaling": {"replicas": 1},
        "storage": {"pvcMode": "new", "newPvc": {"name": "mongo-data", "size": "5Gi", "accessMode": "ReadWriteOnce"}},
        "environment": {
            "envVars": [
                {"name": "MONGO_INITDB_ROOT_USERNAME", "value": ""},
                {"name": "MONGO_INITDB_ROOT_PASSWORD", "value": ""},
                {"name": "MONGO_INITDB_DATABASE", "value": ""},
            ],
            "configMapRefs": [{"name": "mongo-config", "keys": []}],
            "secretRefs": [{"name": "mongo-secrets", "keys": []}],
        },
    },
    {
        "id": "rabbitmq",
        "name": "RabbitMQ",
        "description": "Message broker for distributed systems",
        "category": "Messaging",
        "workloadType": "StatefulSet",
        "containers": [{"name": "rabbitmq", "image": "rabbitmq", "tag": "3-management", "pullPolicy": "IfNotPresent", "ports": [5672, 15672]}],
        "resources": {"cpuRequest": "250m", "cpuLimit": "1000m", "memoryRequest": "512Mi", "memoryLimit": "1Gi"},
        "networking": {"service": {"enabled": True, "type": "ClusterIP", "port": 5672, "targetPort": 5672}},
        "scaling": {"replicas": 1},
        "environment": {
            "envVars": [
                {"name": "RABBITMQ_DEFAULT_USER", "value": ""},
                {"name": "RABBITMQ_DEFAULT_PASS", "value": ""},
                {"name": "RABBITMQ_DEFAULT_VHOST", "value": ""},
            ],
            "configMapRefs": [{"name": "rabbitmq-config", "keys": []}],
            "secretRefs": [{"name": "rabbitmq-secrets", "keys": []}],
        },
    },
    {
        "id": "elasticsearch",
        "name": "Elasticsearch",
        "description": "Distributed search and analytics engine",
        "category": "Observability",
        "workloadType": "StatefulSet",
        "containers": [{"name": "elasticsearch", "image": "docker.elastic.co/elasticsearch/elasticsearch", "tag": "8.11.0", "pullPolicy": "IfNotPresent", "ports": [9200]}],
        "environment": {
            "envVars": [
                {"name": "discovery.type", "value": "single-node"},
                {"name": "xpack.security.enabled", "value": "false"},
                {"name": "ES_JAVA_OPTS", "value": ""},
            ],
            "configMapRefs": [{"name": "elasticsearch-config", "keys": []}],
            "secretRefs": [{"name": "elasticsearch-secrets", "keys": []}],
        },
        "resources": {"cpuRequest": "500m", "cpuLimit": "2000m", "memoryRequest": "1Gi", "memoryLimit": "2Gi"},
        "networking": {"service": {"enabled": True, "type": "ClusterIP", "port": 9200, "targetPort": 9200}},
        "scaling": {"replicas": 1},
        "storage": {"pvcMode": "new", "newPvc": {"name": "es-data", "size": "10Gi", "accessMode": "ReadWriteOnce"}},
    },
    {
        "id": "grafana",
        "name": "Grafana",
        "description": "Observability and data visualization platform",
        "category": "Observability",
        "workloadType": "Deployment",
        "containers": [{"name": "grafana", "image": "grafana/grafana", "tag": "latest", "pullPolicy": "IfNotPresent", "ports": [3000]}],
        "resources": {"cpuRequest": "100m", "cpuLimit": "500m", "memoryRequest": "256Mi", "memoryLimit": "512Mi"},
        "networking": {"service": {"enabled": True, "type": "ClusterIP", "port": 3000, "targetPort": 3000}},
        "scaling": {"replicas": 1},
        "healthChecks": {"readiness": {"enabled": True, "type": "http", "path": "/api/health", "port": 3000}},
        "environment": {
            "envVars": [
                {"name": "GF_SECURITY_ADMIN_USER", "value": ""},
                {"name": "GF_SECURITY_ADMIN_PASSWORD", "value": ""},
                {"name": "GF_SERVER_ROOT_URL", "value": ""},
            ],
            "configMapRefs": [{"name": "grafana-config", "keys": []}],
            "secretRefs": [{"name": "grafana-secrets", "keys": []}],
        },
    },
    {
        "id": "prometheus",
        "name": "Prometheus",
        "description": "Monitoring system and time series database",
        "category": "Observability",
        "workloadType": "Deployment",
        "containers": [{"name": "prometheus", "image": "prom/prometheus", "tag": "latest", "pullPolicy": "IfNotPresent", "ports": [9090]}],
        "resources": {"cpuRequest": "250m", "cpuLimit": "1000m", "memoryRequest": "512Mi", "memoryLimit": "1Gi"},
        "networking": {"service": {"enabled": True, "type": "ClusterIP", "port": 9090, "targetPort": 9090}},
        "scaling": {"replicas": 1},
        "storage": {"pvcMode": "new", "newPvc": {"name": "prometheus-data", "size": "10Gi", "accessMode": "ReadWriteOnce"}},
        "environment": {
            "envVars": [
                {"name": "PROMETHEUS_RETENTION", "value": ""},
                {"name": "SCRAPE_INTERVAL", "value": ""},
            ],
            "configMapRefs": [{"name": "prometheus-config", "keys": []}],
            "secretRefs": [{"name": "prometheus-secrets", "keys": []}],
        },
    },
]


def list_templates() -> List[Dict[str, Any]]:
    return [
        {
            "id": t["id"],
            "name": t["name"],
            "description": t["description"],
            "category": t.get("category", "General"),
            "workloadType": t.get("workloadType", "Deployment"),
        }
        for t in TEMPLATES
    ]


def get_template(template_id: str) -> Dict[str, Any] | None:
    for t in TEMPLATES:
        if t["id"] == template_id:
            return dict(t)
    return None
