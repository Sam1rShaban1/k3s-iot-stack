flowchart TB
    subgraph User["User / Publisher (PC)"]
        P["./run_test.sh<br/>publisher binary"]
    end
    subgraph Network["Network Layer"]
        LB[("MetalLB / NodePort<br/>192.168.1.x:1883<br/>localhost:1883")]
    end
    subgraph EMQX["EMQX Broker<br/>(emqx namespace)"]
        EMQX_P[("Port: 1883<br/>Topic: sensors/#")]
    end
    subgraph Benthos["Benthos<br/>(benthos namespace)"]
        B_IN[("MQTT Input<br/>sensors/#")]
        B_PROC[("Pipeline<br/>Processors")]
        B_OUT[("NATS Output<br/>iot.data")]
    end
    subgraph NATS["NATS JetStream<br/>(nats namespace)"]
        STREAM[("Stream: IOT_DATA<br/>Subject: iot.data")]
        CONS["Consumer<br/>metrics-consumer-push<br/>Delivery: iot.consumer.delivery"]
    end
    subgraph Consumer["NATS Consumer<br/>(nats-consumer namespace)"]
        JS[("JetStream<br/>Subscribe")]
        PY[("Python Consumer<br/>nats-py")]
        VM_POST[("HTTP POST<br/>/api/v1/import/prometheus")]
    end
    subgraph VM["VictoriaMetrics<br/>(victoriametrics namespace)"]
        IMPORT["Import Endpoint<br/>:8428"]
        STORE[("Storage<br/>/storage")]
        QUERY[("Query API<br/>/api/v1/query")]
    end
    subgraph Grafana["Grafana<br/>(monitoring namespace)"]
        DS["Datasources<br/>Prometheus<br/>VictoriaMetrics"]
        DASH[("Dashboards<br/>IoT Latency Analysis<br/>IoT Pipeline")]
    end
    %% Data flow
    P -->|MQTT<br/>sensors/data| LB
    LB -->|TCP 1883| EMQX_P
    EMQX_P -->|Subscribe| B_IN
    B_IN -->|Process| B_PROC
    B_PROC -->|Publish| B_OUT
    B_OUT -->|nats://10.42.0.x:4222| STREAM
    STREAM -->|Push| CONS
    CONS -->|Deliver| JS
    JS -->|JSON msg| PY
    PY -->|HTTP| VM_POST
    VM_POST -->|204 No Content| IMPORT
    IMPORT -->|Store| STORE
    QUERY -->|Read| STORE
    DS -->|Query| QUERY
    DASH -->|Display| DS
    style P fill:#e1f5fe
    style LB fill:#fff3e0
    style EMQX fill:#e8f5e9
    style Benthos fill:#fff8e1
    style NATS fill:#f3e5f5
    style Consumer fill:#fce4ec
    style VM fill:#e0f7fa
    style Grafana fill:#f1f8e9
