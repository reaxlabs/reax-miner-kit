docker run -d --name s1-pro-mammoth --gpus device=0 --network host --init --read-only \
  --tmpfs /tmp:rw,exec,nosuid,nodev,size=16g --cap-drop ALL --security-opt no-new-privileges --pids-limit 4096 \
  --shm-size 2g -v <weights dir>:/model:ro -v <token file>:/run/s1m-token:ro \
  -v $PWD/runtimes/s1-fast/fla-configs/rtx-pro-6000-blackwell-server:/opt/reax/fla-configs/rtx-pro-6000-blackwell-server:ro \
  -e S1M_INTERNAL_TOKEN_FILE=/run/s1m-token -e RUNTIME_UDS= -e RUNTIME_HOST=127.0.0.1 -e RUNTIME_PORT=8112 \
  -e REAX_MODEL=mammoth-1 \
  -e REAX_PROFILE=reference -e CIE_LAZY_LOAD=1 -e REAX_MAX_WAIT_MS=4 \
  -e FLA_CACHE_MODE=strict -e FLA_CONFIG_DIR=/opt/reax/fla-configs/rtx-pro-6000-blackwell-server \
  ghcr.io/reaxlabs/reax-engine-ref-mammoth@sha256:3080a593060542bccbdaf7cf0c66593fc182d9753b3facd50206df86d93b8588
