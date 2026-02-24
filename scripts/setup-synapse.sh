#!/bin/bash
# setup-synapse.sh — one-time setup of a local Synapse homeserver.
# Reads VPS_IP, MATRIX_USER, MATRIX_PASSWORD from .env.
# Run as root from the repo directory on the VPS.

set -e

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="$REPO_DIR/.env"
SYNAPSE_DATA="/opt/synapse/data"
SYNAPSE_IMAGE="matrixdotorg/synapse:latest"
SYNAPSE_SERVICE="/etc/systemd/system/synapse.service"

# ------------------------------------------------------------------ #
# Read .env
# ------------------------------------------------------------------ #
get_env() {
    awk -F' *= *' "/^$1/{print \$2}" "$ENV_FILE" | tr -d '[:space:]'
}

VPS_IP=$(get_env VPS_IP)
MATRIX_USER=$(get_env MATRIX_USER)
MATRIX_PASSWORD=$(get_env MATRIX_PASSWORD)
MATRIX_ADMIN_USER=$(get_env MATRIX_ADMIN_USER)
MATRIX_ADMIN_PASSWORD=$(get_env MATRIX_ADMIN_PASSWORD)

if [[ -z "$VPS_IP" ]]; then
    echo "ERROR: VPS_IP not set in .env"
    exit 1
fi
if [[ -z "$MATRIX_PASSWORD" ]]; then
    echo "ERROR: MATRIX_PASSWORD not set in .env"
    exit 1
fi
if [[ -z "$MATRIX_ADMIN_USER" || -z "$MATRIX_ADMIN_PASSWORD" ]]; then
    echo "ERROR: MATRIX_ADMIN_USER and MATRIX_ADMIN_PASSWORD not set in .env"
    exit 1
fi

# Extract localpart from @localpart:server (e.g. @matrixbot:1.2.3.4 -> matrixbot)
BOT_LOCALPART=$(echo "$MATRIX_USER" | sed 's/@\([^:]*\):.*/\1/')

echo "==> VPS_IP:       $VPS_IP"
echo "==> Bot account:  $MATRIX_USER"
echo ""

# ------------------------------------------------------------------ #
# 1. Data directory
# ------------------------------------------------------------------ #
echo "==> Creating $SYNAPSE_DATA"
mkdir -p "$SYNAPSE_DATA"
chown 991:991 "$SYNAPSE_DATA"

# ------------------------------------------------------------------ #
# 2. Generate config (skip if already exists)
# ------------------------------------------------------------------ #
if [[ -f "$SYNAPSE_DATA/homeserver.yaml" ]]; then
    echo "==> homeserver.yaml already exists, skipping generate"
else
    echo "==> Generating Synapse config for server name: $VPS_IP"
    podman run --rm \
        -v "$SYNAPSE_DATA:/data:Z" \
        -e SYNAPSE_SERVER_NAME="$VPS_IP" \
        -e SYNAPSE_REPORT_STATS=no \
        "$SYNAPSE_IMAGE" generate
fi

# ------------------------------------------------------------------ #
# 3. Patch homeserver.yaml — replace listeners block, disable federation
# ------------------------------------------------------------------ #
echo "==> Patching homeserver.yaml"
python3 - "$SYNAPSE_DATA/homeserver.yaml" <<'EOF'
import sys, re

path = sys.argv[1]
with open(path) as f:
    content = f.read()

# Replace the listeners block
listeners_block = """listeners:
  - port: 8008
    tls: false
    type: http
    x_forwarded: false
    bind_addresses: ['0.0.0.0']
    resources:
      - names: [client]
        compress: false
"""
content = re.sub(r'listeners:.*?(?=\n\S|\Z)', listeners_block, content, flags=re.DOTALL)

# Disable federation
if 'federation_domain_whitelist' not in content:
    content += '\nfederation_domain_whitelist: []\n'

with open(path, 'w') as f:
    f.write(content)

print("homeserver.yaml patched OK")
EOF

# ------------------------------------------------------------------ #
# 4. Install systemd service
# ------------------------------------------------------------------ #
echo "==> Installing $SYNAPSE_SERVICE"
cat > "$SYNAPSE_SERVICE" <<EOF
[Unit]
Description=Synapse Matrix Homeserver
After=network.target

[Service]
Type=simple
Restart=on-failure
RestartSec=5s
ExecStartPre=-/usr/bin/podman rm -f synapse
ExecStart=/usr/bin/podman run --rm --name synapse \\
    -v $SYNAPSE_DATA:/data:Z \\
    -p 8008:8008 \\
    $SYNAPSE_IMAGE
ExecStop=/usr/bin/podman stop synapse

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable synapse

# ------------------------------------------------------------------ #
# 5. Start Synapse and wait for it to be ready
# ------------------------------------------------------------------ #
echo "==> Starting Synapse"
systemctl restart synapse

echo -n "==> Waiting for Synapse to be ready"
for i in $(seq 1 30); do
    if curl -sf http://localhost:8008/_matrix/client/versions > /dev/null 2>&1; then
        echo " OK"
        break
    fi
    echo -n "."
    sleep 2
done

if ! curl -sf http://localhost:8008/_matrix/client/versions > /dev/null 2>&1; then
    echo ""
    echo "ERROR: Synapse did not come up after 60s. Check: journalctl -u synapse -f"
    exit 1
fi

# ------------------------------------------------------------------ #
# 6. Register bot account
# ------------------------------------------------------------------ #
echo "==> Registering bot account: $BOT_LOCALPART"
podman run --rm \
    -v "$SYNAPSE_DATA:/data:Z" \
    --network host \
    "$SYNAPSE_IMAGE" register_new_matrix_user \
    -u "$BOT_LOCALPART" \
    -p "$MATRIX_PASSWORD" \
    --no-admin \
    -c /data/homeserver.yaml \
    http://localhost:8008 || echo "(account may already exist, continuing)"

# ------------------------------------------------------------------ #
# 7. Update matrix-agent service to depend on synapse
# ------------------------------------------------------------------ #
AGENT_SERVICE="/etc/systemd/system/matrix-agent.service"
if [[ -f "$AGENT_SERVICE" ]]; then
    echo "==> Adding synapse dependency to matrix-agent.service"
    if ! grep -q "synapse.service" "$AGENT_SERVICE"; then
        sed -i 's/^After=.*/After=network.target synapse.service/' "$AGENT_SERVICE"
        sed -i '/^After=.*synapse/a Requires=synapse.service' "$AGENT_SERVICE"
        systemctl daemon-reload
    fi
fi

# ------------------------------------------------------------------ #
# Done
# ------------------------------------------------------------------ #
echo ""
echo "==> Synapse is running at http://$VPS_IP:8008"
echo ""
echo "Next steps:"
echo "  1. Add to .env:  MATRIX_HOMESERVER = http://$VPS_IP:8008"
echo "  2. Connect Element to http://$VPS_IP:8008 and create your human account"
echo "  3. systemctl restart matrix-agent"
