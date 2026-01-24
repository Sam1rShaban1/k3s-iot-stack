# 🛰️ Raspberry Pi IoT Data Pipeline with K3s

This project automates the deployment of a **fault-tolerant IoT data pipeline** on a Raspberry Pi 4B cluster using **Ansible** and **K3s** (lightweight Kubernetes).

The repository now supports two deployment architectures:

* **Legacy architecture** (`old.yml`) → EMQX, NiFi, Kafka, IoTDB
* **New architecture** (`main.yml`) → EMQX, NiFi, Benthos, Redpanda, VictoriaMetrics

IoT sensor data flows from the edge into the broker, is processed/streamed, and stored in time-series databases. Longhorn provides distributed persistent storage, and MetalLB handles external IPs for ingress from the edge layer.

---

## 🔄 GitOps Workflow with ArgoCD

This repository now supports **GitOps deployment** using ArgoCD for continuous application management:

### 📁 Repository Structure

```
k3s-iot-stack/
├── 📋 ansible/                    # Initial node provisioning
│   ├── main.yml                   # Main playbook for new architecture
│   ├── old.yml                    # Legacy architecture playbook
│   ├── k3s-setup.yml             # K3s cluster installation
│   ├── pi-setup.yml              # Raspberry Pi OS optimization
│   └── inventory.ini             # Ansible inventory
├── 📦 argocd/                     # GitOps application definitions
│   ├── bootstrap/                 # ArgoCD installation
│   │   ├── argocd-install.yaml
│   │   └── kustomization.yaml
│   ├── apps/                      # Application manifests
│   │   ├── metallb/
│   │   ├── longhorn/
│   │   ├── emqx/
│   │   ├── benthos/
│   │   ├── redpanda/
│   │   ├── victoriametrics/
│   │   ├── monitoring/
│   │   └── kustomization.yaml
│   └── root-application.yaml     # Root App of Apps
├── 📁 files/                       # Helm values files
│   ├── metallb-config.yaml
│   ├── longhorn-values.yaml
│   ├── emqx-values.yaml
│   ├── benthos-values.yaml
│   ├── redpanda-values.yaml
│   ├── victoriametrics-values.yaml
│   └── nifi-values.yml
└── 📚 README.md
```

### 🚀 Deployment Workflow

#### Step 1: Initial Node Setup (Ansible)
```bash
# Bootstrap Raspberry Pi nodes with K3s
ansible-playbook -i inventory.ini main.yml
```

#### Step 2: Install ArgoCD
```bash
# Apply ArgoCD installation
kubectl apply -k argocd/bootstrap

# Wait for ArgoCD to be ready
kubectl wait --for=condition=available deployment/argocd-server -n argocd --timeout=300s

# Port-forward to access ArgoCD UI
kubectl port-forward svc/argocd-server -n argocd 8080:80
```

#### Step 3: Deploy Applications (GitOps)
```bash
# Apply root application to bootstrap all services
kubectl apply -f argocd/root-application.yaml
```

### 🎯 GitOps Benefits

* **Declarative Configuration** - All application state defined in Git
* **Automated Sync** - ArgoCD automatically applies changes from Git
* **Version Control** - Complete audit trail of all configuration changes
* **Rollback** - Easy rollback to previous working states
* **Multi-Environment** - Support for dev/staging/prod environments
* **End-to-End Latency Tracking** - Automatic timestamp injection at each service

### 🔧 Configuration Management

* **Helm Values** - All application configurations in `files/` directory
* **K3s Optimized** - All manifests tuned for Raspberry Pi 4B ARM architecture
* **Longhorn Storage** - Persistent storage using Longhorn with ARM support
* **MetalLB IPs** - Pre-configured external IPs (192.168.1.240-250)
* **Latency Monitoring** - Automatic timestamps for performance analysis

### 📊 Latency Tracking

Every IoT message gets enhanced with timestamps at each stage:

```json
{
  "sensor_id": "temp-001",
  "timestamp": 1706101234567,           // Original sensor timestamp
  "value": 23.5,
  "unit": "celsius",
  "emqx_entry_ts": 1706101234578,       // EMQX entry
  "emqx_exit_ts": 1706101234580,        // EMQX exit
  "benthos_entry_ts": 1706101234582,    // Benthos entry
  "benthos_exit_ts": 1706101234585,     // Benthos exit
  "nats_entry_ts": 1706101234587,       // NATS entry
  "nats_exit_ts": 1706101234588,        // NATS exit
  "latency_sensor_to_emqx_ms": 11,       // Sensor → EMQX
  "latency_emqx_to_benthos_ms": 2,       // EMQX → Benthos
  "latency_benthos_to_nats_ms": 2,       // Benthos → NATS
  "end_to_end_latency_ms": 21           // Total end-to-end
}
```

---

## 📌 Legacy Architecture (Ansible Only)

```text
IoT Sensors
    │  (MQTT)
    ▼
 EMQX Broker  <─── MetalLB (external LoadBalancer IP)
    │  (JSON messages)
    ▼
 Apache NiFi (ClusterIP)
    │  (stream processing / transformations)
    ▼
 Apache Kafka (ClusterIP)
    │  (event bus / partitioned stream)
    ▼
 Apache IoTDB (ClusterIP, persistent storage)
```

**Components:**

* **EMQX** → external MQTT entry point.
* **NiFi** → consumes from EMQX, transforms JSON, publishes to Kafka.
* **Kafka** → streaming backbone for IoT events.
* **IoTDB** → time-series storage for processed IoT data.
* **Longhorn** → distributed, fault-tolerant storage for stateful workloads.
* **MetalLB** → provides external IPs for EMQX and optional UIs.
* **K3s** → lightweight Kubernetes distribution tuned for ARM (Raspberry Pi 4B).

**Deployment:**

```bash
# Legacy Ansible-only deployment
ansible-playbook -i inventory.ini old.yml
```

---

## 📌 New Architecture (GitOps with ArgoCD)

```text
IoT Sensors
    │  (MQTT)
    ▼
 EMQX Broker  <─── MetalLB (external LoadBalancer IP)
    │  (JSON messages)
    ▼
 Benthos (ClusterIP)
    │  (stream processing & pub/sub)
    ▼
 NATS JetStreams (ClusterIP)
    │  (lightweight event bus / streaming)
    ▼
 VictoriaMetrics (ClusterIP, persistent storage)
```

**Components:**

* **EMQX** → external MQTT entry point.
* **Benthos** → lightweight stream processor for filtering, batching, or enriching events.
* **NATS JetStreams** → ultra-lightweight streaming event bus with JetStream persistence.
* **VictoriaMetrics** → scalable time-series database for metrics and IoT data.
* **Longhorn** → distributed, fault-tolerant storage.
* **MetalLB** → external IPs for brokers and UIs.
* **K3s** → ARM-optimized Kubernetes cluster.

**Deployment:**

```bash
# Step 1: Bootstrap nodes with Ansible
ansible-playbook -i inventory.ini main.yml

# Step 2: Install ArgoCD
kubectl apply -k argocd/bootstrap

# Step 3: Deploy all applications via GitOps
kubectl apply -f argocd/root-application.yaml
```

---

## ⚡ Features

* **GitOps with ArgoCD** - Continuous application deployment and management
* **Ansible Bootstrap** - Automated Raspberry Pi node provisioning
* **Multi-master + multi-worker** K3s cluster for HA
* **Longhorn** for distributed persistence across Pis
* **MetalLB** for external service IPs
* ARM-optimized Helm charts with tuned CPU/memory requests for Raspberry Pi 4B 8GB
* Modular applications - update individual components via Git commits
* **Monitoring Stack** - Prometheus + Grafana for observability

---

## 🖥️ Prerequisites

* Raspberry Pi 4B (8GB recommended) × **at least 3 nodes** (2 masters + 1 worker minimum).
* Ethernet LAN connection between nodes.
* SSH access enabled on all Pis.
* Control machine (your laptop) with:

  * [Ansible](https://docs.ansible.com/)
  * [kubectl](https://kubernetes.io/docs/tasks/tools/)
  * [Helm](https://helm.sh/)

---

## 🌐 Accessing Services

* **ArgoCD UI** → MetalLB IP `192.168.1.243:80` (port-forward: `kubectl port-forward svc/argocd-server -n argocd 8080:80`)
* **EMQX Broker (MQTT)** → MetalLB IP `192.168.1.241:1883`
* **EMQX Dashboard** → `http://192.168.1.241:18083` (admin/emqxadmin123)
* **Grafana Dashboard** → MetalLB IP `192.168.1.242:3000`
* **Benthos** → Internal (`ClusterIP`), access via port-forward:
  ```bash
  kubectl port-forward svc/benthos 4195:4195 -n benthos
  ```
* **NATS JetStreams** → Internal (`ClusterIP`), used for streaming events
* **NATS Consumer** → Internal service consuming from JetStreams and writing to VictoriaMetrics
* **VictoriaMetrics** → Internal (`ClusterIP`)
  ```bash
  kubectl exec -it <pod> -n <namespace> -- /bin/bash
  ```
* **Longhorn UI** → MetalLB IP assigned to the service

---

## 🔐 Security

* EMQX authentication (username/password or JWT)
* Benthos TLS + authentication if needed
* NATS JetStreams authentication and authorization
* VictoriaMetrics users & roles
* ArgoCD RBAC and SSO integration

---

## 📊 Monitoring

**Complete Cluster & IoT Pipeline Monitoring**

* **Prometheus + Grafana** automatically deployed via ArgoCD
* **K3s Cluster Metrics** - Node health, pods, services, resource usage
* **Pi Hardware Metrics** - CPU, memory, temperature, storage via Node Exporter
* **IoT Service Metrics** - EMQX, NATS, Benthos, VictoriaMetrics performance
* **Longhorn metrics** for storage monitoring
* **EMQX metrics** for MQTT broker performance
* **NATS JetStreams metrics** for streaming performance
* **Custom dashboards** for IoT data pipeline visualization
* **End-to-end latency tracking** with automatic timestamp injection

### 📈 Pre-configured Grafana Dashboards

1. **Pi Cluster Overview** - Hardware and cluster health
2. **IoT Latency Analysis** - End-to-end and per-service latency
3. **K3s Cluster Dashboard** - Kubernetes resource monitoring
4. **EMQX Monitoring** - MQTT broker performance
5. **NATS Monitoring** - Streaming performance metrics
6. **VictoriaMetrics** - Time-series database health

### 🔍 Metrics Collected

**Cluster Infrastructure**
- CPU, memory, disk usage per node
- Pi temperature monitoring
- Network I/O and storage I/O
- Pod and service health status

**IoT Pipeline Performance**
- Message throughput per service
- End-to-end latency (95th, 50th percentiles)
- Per-service latency breakdown
- Error rates and retry counts
- Queue depths and processing times

**Application Metrics**
- EMQX connections, messages, subscriptions
- NATS JetStreams stream statistics
- Benthos processing rates
- VictoriaMetrics query performance

Access Grafana at `http://192.168.1.242:3000` (admin/admin)

---

