# 🚀 Raspberry Pi IoT Data Pipeline with K3s & GitOps

> **Enterprise-grade IoT data pipeline** on Raspberry Pi 4B cluster with **pure GitOps workflow** using ArgoCD, featuring comprehensive observability and automatic scaling.

This project automates the deployment of a **fault-tolerant, high-performance IoT data pipeline** on a Raspberry Pi 4B cluster using **K3s** (lightweight Kubernetes) and **ArgoCD** for GitOps management. The architecture is optimized for ARM64 with end-to-end latency tracking and complete observability stack.

## 🎯 Architecture Overview

```text
📱 IoT Sensors
    │  (MQTT)
    ▼
 🌐 EMQX Broker  <─── MetalLB (LoadBalancer: 192.168.1.241)
    │  (JSON messages + timestamp injection)
    ▼
 ⚡ Benthos (ClusterIP)
    │  (stream processing + latency tracking)
    ▼
 🚀 NATS JetStreams (ClusterIP)
    │  (lightweight streaming + persistence)
    ▼
 📊 VictoriaMetrics (ClusterIP)
    │  (time-series storage)
    ▼
 📈 Grafana Dashboards (LoadBalancer: 192.168.1.242)
```

## 🏗️ Repository Structure

```
k3s-iot-stack/
├── 🎯 ansible/                    # Bootstrap & provisioning
│   ├── main.yml                   # Pure GitOps bootstrap
│   ├── pi-setup.yml              # Pi4 OS optimization
│   ├── k3s-setup.yml             # Multi-master K3s install
│   ├── argocd-bootstrap.yml      # ArgoCD installation
│   └── inventory.ini             # Cluster inventory
├── 📦 argocd/                     # GitOps application manifests
│   ├── apps/                      # Application definitions
│   │   ├── metallb/               # LoadBalancer
│   │   ├── longhorn/              # Distributed storage
│   │   ├── emqx/                  # MQTT broker
│   │   ├── benthos/               # Stream processor
│   │   ├── nats/                  # Messaging
│   │   ├── nats-consumer/         # Data consumer
│   │   ├── victoriametrics/       # Time-series DB
│   │   ├── monitoring/            # Prometheus + Grafana
│   │   ├── loki/                  # Log aggregation
│   │   ├── promtail/              # Log collection
│   │   ├── tempo/                 # Distributed tracing
│   │   ├── otel-collector/        # OpenTelemetry
│   │   └── kustomization.yaml
│   └── root-application.yaml     # App of Apps
├── 📁 files/                       # Helm values & configs
│   ├── metallb-config.yaml        # IP pool config
│   ├── longhorn-values.yaml       # Storage tuning
│   ├── longhorn-storageclass.yaml # Storage class
│   ├── emqx-values.yaml           # MQTT broker config
│   ├── benthos-values.yaml        # Stream processor config
│   ├── nats-values.yaml           # Messaging config
│   ├── victoriametrics-values.yaml # TSDB config
│   ├── otel-collector-values.yaml # Observability config
│   └── monitoring-configmap.yaml  # Dashboard configs
├── 📊 dashboards/                  # Grafana dashboards
│   ├── k3s-cluster-overview.json  # Cluster health
│   ├── iot-pipeline-comprehensive.json # Pipeline metrics
│   ├── logs-analysis.json         # Log analysis
│   ├── traces-analysis.json       # Distributed tracing
│   ├── pod-resource-analysis.json # Pod resources
│   └── storage-health.json        # Storage monitoring
└── 📚 README.md
```

## 🚀 Quick Start

### Prerequisites

- **Hardware**: Raspberry Pi 4B (8GB) × 3+ nodes
- **Network**: Gigabit Ethernet switch
- **Storage**: 64GB SD cards (recommended)
- **Control Machine**: Ansible, kubectl, Helm installed

### Step 1: Bootstrap Cluster

```bash
# Clone repository
git clone https://github.com/Sam1rShaban1/k3s-iot-stack.git
cd k3s-iot-stack

# Update inventory.ini with your Pi IPs
vim ansible/inventory.ini

# Bootstrap entire cluster (K3s + ArgoCD)
ansible-playbook -i ansible/inventory.ini ansible/main.yml
```

### Step 2: Access ArgoCD

```bash
# Port-forward ArgoCD UI
kubectl port-forward svc/argocd-server -n argocd 8080:80

# Get admin password
kubectl get secret argocd-initial-admin-secret -n argocd -o jsonpath="{.data.password}" | base64 -d

# Open: http://localhost:8080
# Username: admin
# Password: [retrieved above]
```

### Step 3: Monitor Deployment

**All applications deploy automatically via GitOps!** 🎉

- MetalLB (LoadBalancer)
- Longhorn (Distributed Storage)
- EMQX (MQTT Broker)
- Benthos (Stream Processing)
- NATS JetStreams (Messaging)
- VictoriaMetrics (Time-Series DB)
- Complete Observability Stack

## 📊 Service Access

| Service | URL | Credentials |
|---------|-----|-------------|
| **ArgoCD UI** | `http://localhost:8080` (port-forward) | admin/[password] |
| **EMQX MQTT** | `mqtt://192.168.1.241:1883` | admin/emqxadmin123 |
| **EMQX Dashboard** | `http://192.168.1.241:18083` | admin/emqxadmin123 |
| **Grafana** | `http://192.168.1.242:3000` | admin/admin |
| **Longhorn UI** | `http://192.168.1.245` | - |

## 🔧 Scaling & Management

### Adding New Nodes

```bash
# Add new Pi to inventory.ini
[rpi-worker]
rpi-worker3 ansible_host=192.168.1.104 ansible_user=pi

# Bootstrap only new node
ansible-playbook -i ansible/inventory.ini ansible/main.yml --limit rpi-worker3
```

**Automatic Scaling Benefits:**
- ✅ **Zero-touch deployment** - New nodes get all apps automatically
- ✅ **Auto-rebalancing** - Workloads distribute across cluster
- ✅ **Storage replication** - Longhorn replicates to new nodes
- ✅ **Self-healing** - Failed pods auto-recreate

### Configuration Updates

```bash
# Update any values in files/ directory
vim files/emqx-values.yaml

# Commit and push
git add files/emqx-values.yaml
git commit -m "Update EMQX configuration"
git push origin main

# ArgoCD automatically applies changes!
```

## 📈 Performance & Resource Analysis

### 3-Node Cluster (2 Masters + 1 Worker)

**Resource Allocation:**
- **Total CPU**: 12 cores @ 1.5GHz
- **Total RAM**: 24GB
- **Available**: ~20GB (after OS overhead)
- **Storage**: 192GB total (64GB × 3)

**Resource Usage:**
- **CPU Utilization**: ~45%
- **RAM Utilization**: ~65%
- **Storage Utilization**: ~35%

### 5+ Node Cluster Scaling

**Enhanced Performance:**
- **Total CPU**: 20+ cores @ 1.5GHz
- **Total RAM**: 40GB+
- **Available**: ~32GB+
- **Storage**: 320GB+ (64GB × 5+)

**Scaling Benefits:**
- ✅ **Better throughput** - More processing power
- ✅ **Higher availability** - Tolerate node failures
- ✅ **Improved storage** - 3-way replication
- ✅ **Load distribution** - Even workload spread

## 🔍 Complete Observability Stack

### 📊 Grafana Dashboards

**6 Beautiful, Comprehensive Dashboards:**

1. **K3s Cluster Overview** - Node health, CPU, memory, temperature
2. **IoT Pipeline Comprehensive** - End-to-end latency, throughput, performance
3. **Logs Analysis** - Log volume, error tracking, real-time log streaming
4. **Traces Analysis** - Distributed tracing, service dependencies, bottlenecks
5. **Pod Resource Analysis** - Detailed pod resource monitoring
6. **Storage Health** - Longhorn volumes, PVC usage, disk I/O

### 🎯 Observability Features

**Three Pillars of Observability:**

**📈 Metrics (Prometheus)**
- Cluster infrastructure metrics
- IoT service performance
- Hardware monitoring (CPU, memory, temperature)
- Custom business metrics (latency, throughput)

**📋 Logs (Loki)**
- K3s component logs
- IoT service application logs
- System logs (syslog, kern.log)
- Container logs with metadata

**🔍 Traces (Tempo)**
- End-to-end request tracing
- Service dependency mapping
- Performance bottleneck identification
- OpenTelemetry standard compliance

### 🚀 Latency Tracking

Every IoT message gets enhanced with timestamps:

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

## ⚡ Key Features

### 🏗️ Architecture Benefits

- **GitOps Workflow** - All configuration in Git, automatic deployment
- **ARM64 Optimized** - Tuned for Raspberry Pi 4B performance
- **High Availability** - Multi-master K3s with fault tolerance
- **Distributed Storage** - Longhorn with 3-way replication
- **Load Balancing** - MetalLB for external service access
- **Auto-Scaling** - Seamless node addition and workload rebalancing

### 🚀 Performance Optimizations

- **CPU Tuning** - Optimized thread pools and processing
- **Memory Management** - Efficient memory allocation and limits
- **Network Optimization** - Gigabit network tuning for high throughput
- **Storage Performance** - Filesystem and I/O optimizations
- **Batch Processing** - Configurable batching for high throughput

### 🔧 Management Features

- **Zero-Touch Deployment** - New nodes automatically get all applications
- **Self-Healing** - Failed pods automatically recreated
- **Rollback Capability** - Instant rollback to previous working state
- **Version Control** - Every change tracked in Git history
- **Multi-Environment** - Support for dev/staging/prod environments

## 🛠️ Configuration Details

### Helm Values Optimization

All applications are optimized for Raspberry Pi 4B 8GB:

- **Resource Requests** - Balanced CPU/memory allocation
- **Affinity Rules** - Pod distribution across nodes
- **Tolerations** - ARM64 architecture support
- **Performance Tuning** - Thread pools, batching, connections

### Network Configuration

- **MetalLB IP Pool**: 192.168.1.240-250
- **Internal Services**: ClusterIP for security
- **External Services**: LoadBalancer for ingress
- **Network Policies**: Secure inter-service communication

### Storage Configuration

- **Longhorn StorageClass**: `longhorn` (default)
- **Replication Count**: 3-way for durability
- **Filesystem**: ext4 with Pi4 optimizations
- **Backup**: Optional NFS backup target

## 🔐 Security Considerations

- **Authentication**: EMQX username/password, NATS auth
- **Network Policies**: Inter-service traffic control
- **RBAC**: Kubernetes role-based access control
- **Secrets Management**: Kubernetes secrets for credentials
- **TLS**: Optional TLS for service communication

## 📚 Development & Contributing

### Local Development

```bash
# Set up local k3d cluster for testing
k3d cluster create --config k3d-config.yaml

# Deploy applications locally
kubectl apply -k argocd/apps

# Test changes
vim files/emqx-values.yaml
kubectl apply -f argocd/apps/emqx/application.yaml
```

### Contributing Guidelines

1. **Fork** the repository
2. **Create** feature branch
3. **Test** changes on local cluster
4. **Commit** with descriptive messages
5. **Push** and create Pull Request

## 📞 Support & Troubleshooting

### Common Issues

**Node Not Joining Cluster:**
```bash
# Check K3s status
sudo systemctl status k3s-agent

# Check network connectivity
ping <master-ip>

# Check token validity
cat /var/lib/rancher/k3s/server/node-token
```

**Pod Not Starting:**
```bash
# Check pod status
kubectl describe pod <pod-name> -n <namespace>

# Check events
kubectl get events --sort-by=.metadata.creationTimestamp

# Check logs
kubectl logs <pod-name> -n <namespace>
```

**Storage Issues:**
```bash
# Check Longhorn status
kubectl get longhorn volumes
kubectl get longhorn nodes

# Check storage class
kubectl get storageclass
```

### Performance Tuning

**High CPU Usage:**
- Check resource requests/limits
- Monitor pod distribution
- Consider adding more nodes

**High Memory Usage:**
- Check memory leaks in applications
- Adjust memory limits
- Monitor garbage collection

**Network Bottlenecks:**
- Check network configuration
- Monitor bandwidth usage
- Optimize batching settings

## 📄 License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## 🙏 Acknowledgments

- **K3s** - Lightweight Kubernetes distribution
- **ArgoCD** - GitOps continuous delivery
- **EMQX** - MQTT broker for IoT
- **Benthos** - Stream processing
- **NATS** - Cloud-native messaging
- **VictoriaMetrics** - Time-series database
- **Longhorn** - Distributed storage
- **Grafana** - Observability platform

---

## 🎉 Ready to Deploy?

**Start your IoT data pipeline in minutes:**

```bash
# 1. Clone and configure
git clone https://github.com/Sam1rShaban1/k3s-iot-stack.git
cd k3s-iot-stack
vim ansible/inventory.ini

# 2. Bootstrap cluster
ansible-playbook -i ansible/inventory.ini ansible/main.yml

# 3. Access Grafana
echo "🚀 Grafana: http://192.168.1.242:3000 (admin/admin)"
echo "🌐 EMQX: http://192.168.1.241:18083 (admin/emqxadmin123)"
echo "📊 ArgoCD: kubectl port-forward svc/argocd-server -n argocd 8080:80"
```

**Your enterprise-grade IoT data pipeline is ready!** 🚀

