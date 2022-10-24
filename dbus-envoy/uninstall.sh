#!/bin/bash
SCRIPT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )
SERVICE_NAME=$(basename $SCRIPT_DIR)

# sed not working as expected
sed -i ".bak" "/$SERVICE_NAME/d" /data/rc.local > /data/rc.local
rm /service/$SERVICE_NAME
kill $(pgrep -f "supervise $SERVICE_NAME")
chmod a-x $SCRIPT_DIR/service/run
./restart.sh
