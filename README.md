# 🛰️ Raspberry Pi IoT Data Pipeline with K3s

This project automates the deployment of a **fault-tolerant IoT data pipeline** on a Raspberry Pi 4B cluster using **Ansible** and **K3s** (lightweight Kubernetes).

The repository now supports two deployment architectures:

* **Legacy architecture** (`old.yml`) → EMQX, NiFi, Kafka, IoTDB
* **New architecture** (`main.yml`) → EMQX, NiFi, Benthos, Redpanda, VictoriaMetrics

IoT sensor data flows from the edge into the broker, is processed/streamed, and stored in time-series databases. Longhorn provides distributed persistent storage, and MetalLB handles external IPs for ingress from the edge layer.

---

## 📌 Legacy Architecture (`old.yml`)

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
ansible-playbook -i inventory.ini old.yml
```

---

## 📌 New Architecture (`main.yml`)

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
    │  (event bus / partitioned stream)
    ▼
 VictoriaMetrics (ClusterIP, persistent storage)
```

**Components:**

* **EMQX** → external MQTT entry point.
* **NiFi** → consumes from EMQX, transforms JSON, publishes to Benthos/Redpanda.
* **Benthos** → lightweight stream processor for filtering, batching, or enriching events.
* **NATS JetStreams** → Kafka-compatible streaming event bus with lower latency and simpler operations.
* **VictoriaMetrics** → scalable time-series database for metrics and IoT data.
* **Longhorn** → distributed, fault-tolerant storage.
* **MetalLB** → external IPs for brokers and UIs.
* **K3s** → ARM-optimized Kubernetes cluster.

**Deployment:**

```bash
ansible-playbook -i inventory.ini main.yml
```

---

## ⚡ Features (Both Architectures)

* Fully automated **Ansible** deployment.
* **Multi-master + multi-worker** K3s cluster for HA.
* **Longhorn** for distributed persistence across Pis.
* **MetalLB** for external service IPs.
* ARM-optimized Helm charts with tuned CPU/memory requests for Raspberry Pi 4B 8GB.
* Modular playbooks → re-run only the component you want (e.g., update EMQX).

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

* **EMQX Broker (MQTT)** → MetalLB IP (e.g., `192.168.1.240:1883`)
* **NiFi** → Internal (`ClusterIP`), access via port-forward if needed:

  ```bash
  kubectl port-forward svc/nifi 8080:8080 -n nifi
  ```
* **Kafka / NATS JetStreams** → Internal (`ClusterIP`), used for streaming events
* **IoTDB / VictoriaMetrics** → Internal (`ClusterIP`)

  ```bash
  kubectl exec -it <pod> -n <namespace> -- /bin/bash
  ```
* **Longhorn UI** → MetalLB IP assigned to the service

---

## 🔐 Security (optional)

* EMQX authentication (username/password or JWT)
* NiFi TLS + user logins
* Kafka/Redpanda SASL/SSL if needed
* IoTDB/VictoriaMetrics users & roles

---

## 📊 Monitoring (optional)

* Deploy **Prometheus + Grafana** via Helm for metrics collection.
* Visualize broker load, message throughput, and storage usage.

---

