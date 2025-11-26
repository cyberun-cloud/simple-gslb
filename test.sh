#!/bin/bash
set -e

GREEN='\033[0;32m'
BLUE='\033[0;34m'
RED='\033[0;31m'
NC='\033[0m'

log() {
    echo -e "${GREEN}[TEST] $1${NC}"
}

wait_for_pod() {
    log "Waiting for $1 to be ready..."
    kubectl wait --namespace test-gslb \
        --for=condition=ready pod \
        --selector=$2 \
        --timeout=90s
}

# 0. Check necessary tools
command -v minikube >/dev/null 2>&1 || { echo "Please install minikube first"; exit 1; }
command -v helm >/dev/null 2>&1 || { echo "Please install helm first"; exit 1; }

# 1. Start minikube
log "Step 1: Checking and starting Minikube..."
if ! minikube status | grep -q "Running"; then
    minikube start
else
    log "Minikube is already running"
fi

# 2. Set Docker environment
log "Step 2: Pointing to Minikube Docker Daemon..."
eval $(minikube docker-env)

# 3. Build Image
log "Step 3: Building SimpleGSLB Docker Image..."
# Use the default repo name from values.yaml
docker build -t myrepo/simplegslb:latest .

# 4. Create Namespace
log "Step 4: Creating Namespace 'test-gslb'..."
kubectl create ns test-gslb --dry-run=client -o yaml | kubectl apply -f -

# 5. Install Helm Chart
log "Step 5: Installing SimpleGSLB Helm Chart..."
helm upgrade --install simplegslb ./charts/simplegslb \
    --namespace test-gslb \
    --set image.pullPolicy=Never \
    --set interval=5 \
    --set controller.geoip=true \
    --set geoip.account=$GEOIP_ACCOUNT \
    --set geoip.license=$GEOIP_LICENSE

wait_for_pod "SimpleGSLB Controller" "app=simplegslb-simplegslb"

# 6. Create Targets (Simulate Real Services)
log "Step 6: Deploying test Targets (Nginx & Valkey)..."

# 6.1 Nginx (HTTP Target)
kubectl create deployment nginx --image=nginx:alpine -n test-gslb --dry-run=client -o yaml | kubectl apply -f -
kubectl expose deployment nginx --port=80 -n test-gslb --dry-run=client -o yaml | kubectl apply -f -

# 6.2 Valkey (TCP Target)
kubectl create deployment valkey --image=valkey/valkey:alpine -n test-gslb --dry-run=client -o yaml | kubectl apply -f -
kubectl expose deployment valkey --port=6379 -n test-gslb --dry-run=client -o yaml | kubectl apply -f -

wait_for_pod "Nginx" "app=nginx"
wait_for_pod "Valkey" "app=valkey"

NGINX_IP=$(kubectl get svc nginx -n test-gslb -o jsonpath='{.spec.clusterIP}')
VALKEY_IP=$(kubectl get svc valkey -n test-gslb -o jsonpath='{.spec.clusterIP}')
log "Obtained Target IPs - Nginx: $NGINX_IP, Valkey: $VALKEY_IP"

# 7. Create CRD
log "Step 7: Creating GslbConfig CRD..."
cat <<EOF | kubectl apply -f -
apiVersion: cyberun.cloud/v1
kind: GslbConfig
metadata:
  name: test-config
  namespace: test-gslb
spec:
  domain: "cloud.example.com"
  nameservers:
    - hostname: "ns1.example.com"
      address: 1.1.1.1
  records:
    - name: "app"
      targets:
        # GeoDNS Check Test
        - address: 8.8.8.8
          location: "XX"
          weight: 1
          protocol: "tcp"
          port: 53

        # HTTP Check Test
        - address: "$NGINX_IP"
          # location: "default"
          weight: 1
          protocol: "http"
          port: 80
          path: "/"
        
        # TCP Check Test
        - address: "$VALKEY_IP"
          # location: "default"
          weight: 1
          protocol: "tcp"
          port: 6379
EOF

log "Waiting for Controller to perform the first health check (about 10 seconds)..."
sleep 10

# 8. Run Pod to perform DNS query
log "Step 8: Starting Client Pod to perform DNS query..."
DNS_SVC_IP=$(kubectl get svc simplegslb-simplegslb -n test-gslb -o jsonpath='{.spec.clusterIP}')

# 9. Print results
echo -e "\n${BLUE}================ Test Results ================${NC}"
echo "DNS Server IP: $DNS_SVC_IP"
echo "Query Domain: app.cloud.example.com"
echo "Expected Result: Should contain $NGINX_IP (HTTP) and $VALKEY_IP (TCP)"
echo -e "${BLUE}----------------------------------------------${NC}"

kubectl run client-test --rm -i --restart=Never \
    --image=alpine:3.18 -n test-gslb \
    -- sh -c "apk add --quiet --no-cache bind-tools > /dev/null && \
              echo '>>> Dig Result:' && \
              dig @$DNS_SVC_IP app.cloud.example.com +short"

echo -e "${BLUE}==============================================${NC}"

# 9. Advanced Test: Simulate Failure
log "Step 9 (Advanced): Simulating Nginx failure (Scale to 0)..."
kubectl scale deployment nginx --replicas=0 -n test-gslb
log "Waiting for Controller to detect failure (about 10 seconds)..."
sleep 10

echo -e "\n${RED}>>> Failure Simulation Test Result (Nginx should disappear):${NC}"
kubectl run client-fail-test --rm -i --restart=Never \
    --image=alpine:3.18 -n test-gslb \
    -- sh -c "apk add --quiet --no-cache bind-tools > /dev/null && \
              dig @$DNS_SVC_IP app.cloud.example.com +short"

# 9.5. Inspect CoreDNS Config and Zones
log "Step 9.5: Inspecting CoreDNS Corefile and zones..."

POD_NAME=$(kubectl get pods -n test-gslb -l app=simplegslb-simplegslb -o jsonpath='{.items[0].metadata.name}')

log "Printing /etc/coredns/Corefile:"
kubectl exec -n test-gslb "$POD_NAME" -- cat /etc/coredns/Corefile

log "Listing files in /etc/coredns/zones:"
kubectl exec -n test-gslb "$POD_NAME" -- ls -l /etc/coredns/zones

for file in $(kubectl exec -n test-gslb "$POD_NAME" -- ls /etc/coredns/zones); do
  log "Printing /etc/coredns/zones/$file:"
  kubectl exec -n test-gslb "$POD_NAME" -- cat /etc/coredns/zones/"$file"
done

# 10. Cleanup Environment
log "Step 10: Cleaning up environment (Deleting Namespace)..."
helm uninstall simplegslb -n test-gslb
kubectl delete ns test-gslb

log "Test completed!"
