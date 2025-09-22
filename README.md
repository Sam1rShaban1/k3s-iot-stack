# 🛰️ Raspberry Pi IoT Data Pipeline with K3s, EMQX, NiFi, Kafka, IoTDB, Longhorn, and MetalLB

This project automates the deployment of a **fault-tolerant IoT data pipeline** on a Raspberry Pi 4B cluster using **Ansible** and **K3s** (lightweight Kubernetes).

IoT sensor data flows from the edge into EMQX, is processed by NiFi, streamed via Kafka, and stored in IoTDB. Longhorn provides distributed persistent storage, and MetalLB handles external IPs for ingress from the edge layer.

---

## 📌 Architecture

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

-   **EMQX** → external entry point for IoT devices (MQTT).
-   **NiFi** → consumes from EMQX, transforms JSON, publishes to Kafka.
-   **Kafka** → streaming backbone for IoT events.
-   **IoTDB** → time-series storage for processed IoT data.
-   **Longhorn** → distributed, fault-tolerant storage for stateful workloads.
-   **MetalLB** → provides external IPs for EMQX and optional UIs.
-   **K3s** → lightweight Kubernetes distribution tuned for ARM (Raspberry Pi 4B).

---

## ⚡ Features

-   Fully automated cluster setup with **Ansible**.
-   **Multi-master + multi-worker** k3s cluster for high availability.
-   **Longhorn storage** for distributed persistence across Pis.
-   **MetalLB** for external service IPs on your LAN.
-   ARM-optimized Helm charts with tuned CPU/memory requests for Raspberry Pi 4B 8GB.
-   Modular playbooks → re-run only the component you want (e.g. update EMQX).

---

## 🖥️ Prerequisites

-   Raspberry Pi 4B (8GB recommended) × **at least 3 nodes** (2 masters + 1 worker minimum).
-   Pis connected via **Ethernet switch** on the same LAN.
-   SSH access enabled on all Pis.
-   A control machine (your laptop) with:
    -   [Ansible](https://docs.ansible.com/)
    -   [kubectl](https://kubernetes.io/docs/tasks/tools/)
    -   [Helm](https://helm.sh/)

---

## 📂 Repository Structure

```text
ansible/
├── main.yml                # Master playbook (runs all)
├── pi-setup.yml            # Prepare Pis (update, cgroups, swap off)
├── k3s-setup.yml           # Install k3s (masters + workers)
├── metallb.yml             # Install MetalLB
├── longhorn.yml            # Install Longhorn
├── emqx.yml                # Deploy EMQX broker
├── nifi.yml                # Deploy NiFi
├── kafka.yml               # Deploy Kafka
├── iotdb.yml               # Deploy IoTDB
├── inventory.ini           # Define your cluster nodes
└── files/
    ├── metallb-config.yaml
    ├── longhorn-values.yaml
    ├── longhorn-storageclass.yaml
    ├── emqx-values.yaml
    ├── nifi-values.yaml
    ├── kafka-values.yaml
    └── iotdb-values.yaml
```

---

## ⚙️ Setup & Deployment

### 1. Clone the repo

```bash
git clone [https://github.com/Sam1rShaban1/k3s-iot-stack.git]
cd ansible/
```

### 2. Configure inventory

Edit `inventory.ini` with your Pi hostnames / IPs:

```ini
[rpi-master]
rpi-master1 ansible_host=192.168.1.100 ansible_user=pi
rpi-master2 ansible_host=192.168.1.101 ansible_user=pi

[rpi-worker]
rpi-worker1 ansible_host=192.168.1.102 ansible_user=pi
rpi-worker2 ansible_host=192.168.1.103 ansible_user=pi
```

### 3. Configure MetalLB IP pool

Edit `files/metallb-config.yaml` to match your LAN. Make sure the range is **outside your router’s DHCP range**.

Example:

```yaml
addresses:
  - 192.168.1.240-192.168.1.250
```

### 4. Run the full deployment

```bash
ansible-playbook -i inventory.ini main.yml
```

This will:

1.  Prepare Pis (`pi-setup.yml`).
2.  Install k3s masters + workers (`k3s-setup.yml`).
3.  Install MetalLB (`metallb.yml`).
4.  Install Longhorn (`longhorn.yml`).
5.  Deploy EMQX, NiFi, Kafka, and IoTDB (`*.yml`).

---

## 🌐 Accessing Services

-   **EMQX Broker (MQTT)** → Accessible at the MetalLB IP (e.g. `192.168.1.240:1883`).
-   **NiFi** → Internal only (`ClusterIP`), access via port-forward if needed:
    ```bash
    kubectl port-forward svc/nifi 8080:8080 -n nifi
    ```
-   **Kafka** → Internal only (`ClusterIP`), used by the NiFi → IoTDB pipeline.
-   **IoTDB** → Internal only (`ClusterIP`), query from within the cluster:
    ```bash
    kubectl exec -it <iotdb-pod> -n iotdb -- /iotdb/bin/sqlline
    ```
-   **Longhorn UI** → Available via a MetalLB IP assigned to the Longhorn service.

---

## 🔐 Security (optional)

-   Enable EMQX authentication (username/password or JWT).
-   Secure NiFi with TLS and user logins.
-   Configure Kafka SASL/SSL if external clients are needed.
-   Configure IoTDB users and roles.

---

## 📊 Monitoring (optional)

Deploy **Prometheus + Grafana** via Helm to collect metrics:

-   EMQX, NiFi, Kafka, and IoTDB all expose monitoring endpoints.
-   Visualize broker load, message throughput, storage usage, etc.

---

## 🚀 Next Steps

-   Import a NiFi flow template to connect:
    -   **MQTT (EMQX) → JSON Processing → Kafka → IoTDB**.
-   Optionally automate flow deployment using the NiFi REST API.
-   Add Grafana dashboards for real-time pipeline monitoring.

---
