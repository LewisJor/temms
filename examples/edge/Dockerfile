FROM ubuntu:24.04@sha256:68ee22004a9109b44973313b02ff7b1de250f1de0f5ecb628d73f579aaff33ac

RUN apt-get update \
    && apt-get install --yes --no-install-recommends python3 python3-venv ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && python3 -m venv /opt/venv

ENV PATH="/opt/venv/bin:${PATH}" \
    HOME=/tmp \
    OPENBLAS_NUM_THREADS=1 \
    OMP_NUM_THREADS=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Install the built distribution, never the source checkout or an editable package.
COPY dist/temms-0.2.0a1-py3-none-any.whl /tmp/wheels/
RUN python -m pip install --no-cache-dir \
      '/tmp/wheels/temms-0.2.0a1-py3-none-any.whl[onnx,demo]' \
      numpy==2.4.2 onnxruntime==1.24.1 Pillow==12.1.0 \
    && python -m pip check \
    && rm -rf /tmp/wheels

WORKDIR /opt/app
COPY examples/live_swap/*.py examples/live_swap/*.json ./examples/live_swap/
COPY examples/edge/*.py ./examples/edge/

USER 65532:65532
CMD ["python", "-m", "examples.edge.run"]
