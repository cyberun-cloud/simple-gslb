# SimpleGSLB

**SimpleGSLB** is a lightweight, Kubernetes-native Global Server Load Balancing (GSLB) solution designed specifically for **BareMetal clusters**, **Hybrid Clouds**, and environments dependent on **External Load Balancers**.

It acts as a self-hosted authoritative DNS controller that **actively monitors target health** (HTTP/TCP) and dynamically updates CoreDNS records based on `GslbConfig` Custom Resource Definitions (CRDs), offering intelligent **Geo-Location Routing** and **Failover**.

---

## 🚀 The Problem (Why this exists)

In managed cloud environments (AWS Route53, Google Cloud DNS), GSLB is seamless because Load Balancers are tightly integrated with DNS. However, **On-Premise** or **BareMetal** setups often face a specific "automation gap" that existing open-source tools fail to address:

1.  **Decoupled Entry Points:** Your cluster's entry point is often a BGP Anycast IP, a Firewall VIP, or an external hardware Load Balancer (F5, HAProxy) that isn't directly managed by Kubernetes Service resources.
2.  **Ingress Status Unreliability:** Tools like **k8gb** rely on the Kubernetes Ingress status. In many BareMetal setups, this status might report internal IPs unreachable from the public internet, breaking automation.
3.  **The "Silent Failure" of ExternalDNS:** Popular tools like **ExternalDNS** are excellent for syncing configuration, but they **lack active health checks**. If your static endpoint goes down, ExternalDNS continues to serve the dead IP, causing outages.

**SimpleGSLB was built to fill this specific gap.** It creates a bridge where you manually define your external targets, and the controller handles the liveness verification and traffic routing logic.

## 🎯 Who is this for?

SimpleGSLB is the ideal solution if:

- You run **BareMetal K8s** clusters and use MetalLB, Keepalived, or BGP.
- You have **Hybrid Cloud** infrastructure and need a unified DNS entry point for traffic distribution.
- You need to route traffic based on **Geo-Location** (Split-Horizon DNS) without paying for enterprise hardware or managed services.
- You need **Active Health Checks** for static IPs that are not directly tied to a K8s Pod's lifecycle.

## ✨ Key Features

- **Decoupled Target Management:** Define any IP address as a target via CRDs, regardless of whether it belongs to the cluster or an external legacy system.
- **Active Health Checking:**
  - **HTTP/HTTPS:** Probes endpoints and validates status codes (2xx).
  - **TCP:** Verifies port connectivity.
  - _Result:_ Unhealthy targets are automatically removed from DNS responses to prevent traffic blackholing.
- **Smart Routing Strategies:**
  - **GeoDNS:** Returns the nearest endpoint based on the client's source IP.
  - **Weighted Round-Robin:** Distribute traffic load (e.g., Primary DC vs. Backup DC).
  - **Automatic Fallback:** If a specific region fails, traffic automatically fails over to a global default pool.

## 🆚 Alternatives & When to Use What

Choosing the right tool depends entirely on your infrastructure constraints.

| Solution        | Best For...                                                       | The Limitation                                                                                                                                           |
| :-------------- | :---------------------------------------------------------------- | :------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **ExternalDNS** | Syncing K8s Services to Managed DNS (Route53, Cloudflare).        | **No Health Checks.** It assumes if the K8s Service exists, the IP is valid. It cannot detect if the actual path to that IP is broken.                   |
| **k8gb**        | Automated GSLB for fully Cloud Native environments using Ingress. | **Tightly Coupled.** Hard to use if your public IP is decoupled from the Ingress resource or managed manually by a network team.                         |
| **MetalLB**     | Layer 2/BGP Load Balancing _within_ a cluster.                    | **Local Only.** It provides a VIP for a single cluster. It does not handle _Global_ DNS routing or failover between geographically distributed clusters. |
| **SimpleGSLB**  | **Self-hosted DNS + Health Checks for Static/External IPs.**      | **Manual Definition.** You must define the Target IPs in the CRD. It is designed for scenarios where you _need_ that control.                            |

## ⚙️ Configuration Example

Manage your Global DNS routing purely via Kubernetes CRDs.

**Scenario:**

- **Region A Users:** Route to `1.1.1.1` (Port 80 HTTP Check).
- **Region B Users:** Route to `2.2.2.2` (Port 443 TCP Check).
- **Everyone Else (or if regions fail):** Fallback to `8.8.8.8`.

<!-- end list -->

```yaml
apiVersion: cyberun.cloud/v1
kind: GslbConfig
metadata:
  name: my-global-app
spec:
  domain: "app.example.com"
  nameservers:
    - hostname: "ns1.example.com"
      address: "192.168.1.50"
  records:
    - name: "www"
      targets:
        # --- Region A Node ---
        - address: "1.1.1.1"
          location: "US" # ISO 3166-1 Alpha-2 Code
          weight: 10
          protocol: "http"
          port: 80
          path: "/healthz"

        # --- Region B Node ---
        - address: "2.2.2.2"
          location: "JP"
          weight: 10
          protocol: "tcp" # TCP Connect Check
          port: 443

        # --- Global Fallback Node ---
        - address: "8.8.8.8"
          # location: ""        # Empty location = Global Default
          weight: 1
          protocol: "http"
```

## 🏗 Architecture

1.  **GslbConfig CRD**: You define the Domain, Nameservers, Targets (IPs), Geo-Tags, and Health Check rules.
2.  **Controller**:
    - Continuously watches CRDs.
    - Runs concurrent **Async Health Checks** against all targets.
    - Dynamically generates intelligent Zone Files (Views) based on region and health status.
3.  **CoreDNS**:
    - Serves DNS requests.
    - Uses **Split-Horizon** logic (Views) to serve different records based on the requester's IP.
    - Provides high-performance caching.
