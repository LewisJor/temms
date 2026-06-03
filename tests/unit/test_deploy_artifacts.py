"""
Tests for Linux VM deployment artifacts.
"""

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class TestDeployArtifacts:
    """Validate boring deployment assets stay coherent."""

    def test_systemd_unit_uses_config_and_env_file(self):
        service = (ROOT / "deploy" / "temms.service").read_text()

        assert "EnvironmentFile=-/etc/temms/temms.env" in service
        assert "--config /etc/temms/temms.yaml" in service
        assert "--host ${TEMMS_HOST}" in service
        assert "--port ${TEMMS_PORT}" in service
        assert "ReadWritePaths=/var/lib/temms /var/log/temms /etc/temms" in service
        assert "protects control, Hub Lite, and Web UI write endpoints" in service

    def test_env_file_declares_default_host_port_and_data_dir(self):
        env_file = (ROOT / "deploy" / "temms.env").read_text()

        assert "TEMMS_HOST=0.0.0.0" in env_file
        assert "TEMMS_PORT=8080" in env_file
        assert "TEMMS_DATA_DIR=/var/lib/temms" in env_file
        assert "TEMMS_API_TOKEN" in env_file
        assert "protects control, Hub Lite, and Web UI write endpoints" in env_file
        assert "TEMMS_GENERATE_API_TOKEN=false" in env_file
        assert "TEMMS_HUB_URL" in env_file
        assert "TEMMS_HUB_AUTO_APPLY" in env_file
        assert "TEMMS_PACKAGE_SIGNING_KEY_FILE" in env_file

    def test_install_script_creates_runtime_dirs_and_installs_extras(self):
        install = (ROOT / "deploy" / "install.sh").read_text()

        assert "set -euo pipefail" in install
        assert "TEMMS_EXTRAS:-inference" in install
        assert "${DATA_DIR}/benchmarks" in install
        assert 'install -d -o temms -g temms -m 0750 "${CONFIG_DIR}/policies"' in install
        assert 'pip" install "${INSTALL_SOURCE}[${EXTRAS}]"' in install
        assert "deploy/temms.env" in install
        assert "generate_api_token" in install
        assert "TEMMS_GENERATE_API_TOKEN:-true" in install
        assert "TEMMS_API_TOKEN=${token}" in install
        assert "openssl rand -hex 32" in install
        assert "secrets.token_hex(32)" in install
        assert "temms doctor" in install
        assert "render_service_unit" in install
        assert "render_path_template" in install
        assert "install_rendered_template" in install
        assert "sed_escape" in install
        assert "mktemp -d" in install
        assert "trap cleanup_tmp_dir EXIT" in install
        assert "s|/opt/temms|${prefix}|g" in install
        assert "s|/etc/temms|${config_dir}|g" in install
        assert "s|/var/lib/temms|${data_dir}|g" in install
        assert "s|/var/log/temms|${log_dir}|g" in install
        assert (
            'install_rendered_template 0644 root root deploy/temms.conf "${CONFIG_DIR}/temms.yaml"'
            in install
        )
        assert (
            'install_rendered_template 0640 root temms deploy/temms.env "${CONFIG_DIR}/temms.env"'
            in install
        )
        assert '"/etc/systemd/system/${SERVICE_NAME}.service"' in install

    def test_install_script_renders_systemd_unit_for_custom_paths(self):
        install = (ROOT / "deploy" / "install.sh").read_text()

        assert 'prefix="$(sed_escape "${PREFIX}")"' in install
        assert 'config_dir="$(sed_escape "${CONFIG_DIR}")"' in install
        assert 'data_dir="$(sed_escape "${DATA_DIR}")"' in install
        assert 'log_dir="$(sed_escape "${LOG_DIR}")"' in install
        assert "render_path_template deploy/temms.service" in install
        assert '"${template}" > "${output}"' in install
        assert 'echo "  log dir:     ${LOG_DIR}"' in install
        assert 'echo "  service:     /etc/systemd/system/${SERVICE_NAME}.service"' in install

    def test_dockerfile_has_archive_support_and_benchmark_dir(self):
        dockerfile = (ROOT / "Dockerfile").read_text()

        assert "zstd" in dockerfile
        assert "useradd --system --gid temms" in dockerfile
        assert (
            "chown -R temms:temms /var/lib/temms /etc/temms " "/app/src /app/scripts /app/examples"
        ) in dockerfile
        assert "USER temms" in dockerfile
        assert "/var/lib/temms/benchmarks" in dockerfile
        assert "ARG TEMMS_EXTRAS=inference" in dockerfile
        assert 'pip install --no-cache-dir -e ".[${TEMMS_EXTRAS}]"' in dockerfile

    def test_docker_entrypoint_honors_explicit_runtime_commands(self):
        entrypoint = (ROOT / "scripts" / "docker-entrypoint.sh").read_text()

        assert 'if [ "$#" -gt 0 ]; then' in entrypoint
        assert 'exec "$@"' in entrypoint
        assert entrypoint.index('if [ "$#" -gt 0 ]; then') < entrypoint.index("TEMMS Sim Environment")

    def test_docker_compose_sim_opts_into_sim_dependencies(self):
        compose = (ROOT / "docker-compose.yml").read_text()

        assert "TEMMS_EXTRAS: sim" in compose

    def test_docker_entrypoint_imports_unsigned_seed_package(self):
        entrypoint = (ROOT / "scripts" / "docker-entrypoint.sh").read_text()

        assert "temms import /app/examples/package-example/" in entrypoint
        assert "--allow-unsigned-package" in entrypoint
        assert "--default model-yolov8-daylight-001" in entrypoint
        assert "--default-model" not in entrypoint

    def test_docker_acceptance_compose_defines_three_agents(self):
        compose = (ROOT / "deploy" / "docker-compose.acceptance.yml").read_text()

        assert "temms-hub:" in compose
        assert "temms-edge-online:" in compose
        assert "temms-edge-airgap:" in compose
        assert compose.count("TEMMS_EXTRAS: inference") == 3
        assert "TEMMS_HUB_URL: http://temms-hub:8080" in compose
        assert "TEMMS_DEVICE_PROFILE: x86_64-cpu" in compose
        assert "TEMMS_DEVICE_PROFILE: rpi5-tflite" in compose
        assert compose.count('TEMMS_ROLLOUT_REQUIRE_SIGNATURE: "true"') == 3
        assert (
            compose.count("TEMMS_PACKAGE_SIGNING_KEY: ${TEMMS_ACCEPTANCE_SIGNING_KEY:-change-me}")
            == 3
        )
        assert "${TEMMS_ACCEPTANCE_PACKAGE_DIR:-../dist}:/acceptance-packages:ro" in compose
        assert "${TEMMS_ACCEPTANCE_HUB_PORT:-18080}:8080" in compose
        assert "${TEMMS_ACCEPTANCE_ONLINE_PORT:-18081}:8080" in compose
        assert "${TEMMS_ACCEPTANCE_AIRGAP_PORT:-18082}:8080" in compose

    def test_makefile_exposes_docker_acceptance_targets(self):
        makefile = (ROOT / "Makefile").read_text()

        assert "docker-build-runtime:" in makefile
        assert "docker build --platform linux/amd64" in makefile
        assert "temms/agent:inference-amd64" in makefile
        assert "TEMMS_EXTRAS=inference" in makefile
        assert "docker-acceptance:" in makefile
        assert "docker-acceptance-up:" in makefile
        assert "docker-acceptance-down:" in makefile
        assert "deploy/docker-acceptance-run.sh" in makefile
        assert "deploy/docker-compose.acceptance.yml up --build -d" in makefile

    def test_docker_acceptance_runner_creates_packages_and_runs_harness(self):
        runner = (ROOT / "deploy" / "docker-acceptance-run.sh").read_text()

        assert runner.startswith("#!/usr/bin/env bash")
        assert "require_docker" in runner
        assert "docker compose version" in runner
        assert "docker info" in runner
        assert "Docker daemon is not reachable" in runner
        assert "create_acceptance_packages" in runner
        assert "reset_stack" in runner
        assert "down -v" in runner
        assert 'examples" / "package-example" / "models' in runner
        assert "yolov8n-daylight.onnx" in runner
        assert "mobilenet-v2-tiny.onnx" in runner
        assert "sign_package" in runner
        assert "create_package_archive" in runner
        assert "docker compose -f" in runner
        assert "wait_health" in runner
        assert 'multi-vm-acceptance.sh" connected-lab' in runner
        assert (
            'ONLINE_PACKAGE_PATH="/acceptance-packages/${ONLINE_PACKAGE_ID}.temms.tar.zst"'
            in runner
        )
        assert (
            'ONLINE_PACKAGE_HOST_PATH="${PACKAGE_DIR}/${ONLINE_PACKAGE_ID}.temms.tar.zst"' in runner
        )
        assert (
            'AIRGAP_PACKAGE_HOST_PATH="${PACKAGE_DIR}/${AIRGAP_PACKAGE_ID}.temms.tar.zst"' in runner
        )
        assert "container acceptance summary" in runner

    def test_docker_entrypoint_creates_slot_with_current_cli(self):
        entrypoint = (ROOT / "scripts" / "docker-entrypoint.sh").read_text()

        assert "temms slot create vision" in entrypoint
        assert "--default model-yolov8-daylight-001" in entrypoint
        assert "--default-model" not in entrypoint

    def test_multi_vm_acceptance_harness_documents_real_vm_flow(self):
        harness = ROOT / "deploy" / "multi-vm-acceptance.sh"
        text = harness.read_text()

        assert text.startswith("#!/usr/bin/env bash")
        assert "connected-lab" in text
        assert "export-airgap" in text
        assert "import-airgap" in text
        assert "ONLINE_DEVICE_PROFILE:-x86_64-cpu" in text
        assert "AIRGAP_DEVICE_PROFILE:-rpi5-tflite" in text
        assert "/airgap/export" in text
        assert "/airgap/import" in text
        assert "/evidence/export" in text
        assert "/rollouts/${ONLINE_ROLLOUT_ID}/rollback" in text
        assert "SUMMARY_PATH" in text
        assert "temms-mvp-acceptance/v1" in text
        assert "acceptance-summary.json" in text
        assert "result" in text
        assert "needs_attention" in text
        assert "rollout_state_from_evidence" in text
        assert '"sign=false"' in text
        assert "ONLINE_PACKAGE_HOST_PATH" in text
        assert "AIRGAP_PACKAGE_HOST_PATH" in text
        assert 'export ACCEPTANCE_MODE="${mode}"' in text
        assert "export DEPLOYMENT_STATUS_PATH SUMMARY_PATH" in text
        assert '"signing_key=' not in text
        assert "SIGNING_KEY or SIGNING_KEY_FILE" not in text

    def test_linux_vm_guide_references_multi_vm_acceptance_harness(self):
        guide = (ROOT / "docs" / "run-on-linux-vm.md").read_text()

        assert "Real Multi-VM Acceptance Harness" in guide
        assert "deploy/multi-vm-acceptance.sh connected-lab" in guide
        assert "deploy/multi-vm-acceptance.sh export-airgap" in guide
        assert "deploy/multi-vm-acceptance.sh import-airgap" in guide
        assert "central-deployment-status.json" in guide
        assert "online-edge-evidence.json" in guide
        assert "airgap-edge-evidence.json" in guide
        assert "acceptance-summary.json" in guide
        assert "final rollout states" in guide
        assert "make docker-acceptance-up" in guide
        assert "make docker-acceptance" in guide
        assert "/acceptance-packages/pkg-online-x86.temms.tar.zst" in guide
        assert "generated `TEMMS_API_TOKEN`" in guide
        assert "TEMMS_GENERATE_API_TOKEN=false" in guide
        assert "harness does not send it in rollout request bodies" in guide
        assert "PREFIX=/srv/temms" in guide
        assert "systemd unit, `temms.yaml`, and `temms.env`" in guide
        assert "--default model-yolov8-daylight-001" in guide
        assert "--default-model" not in guide
        assert "TEMMS_EXTRAS=inference" in guide
        assert "TEMMS_EXTRAS=sim" in guide

    def test_multi_vm_acceptance_summary_function_runs(self, tmp_path):
        status = tmp_path / "central-status.json"
        status.write_text(
            json.dumps(
                {
                    "rollouts": {
                        "rollout-online": {"state": "rolled_back"},
                        "rollout-airgap": {"state": "assigned"},
                    }
                }
            ),
            encoding="utf-8",
        )
        airgap_evidence = tmp_path / "airgap-evidence.json"
        airgap_evidence.write_text(
            json.dumps(
                {
                    "hub_lite": {
                        "rollouts": {
                            "rollout-airgap": {"state": "activated"},
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        summary = tmp_path / "summary.json"
        script = f"""
set -euo pipefail
set -- help
source {ROOT / "deploy" / "multi-vm-acceptance.sh"} >/dev/null
ACCEPTANCE_DIR={tmp_path} \
DEPLOYMENT_STATUS_PATH={status} \
ONLINE_EVIDENCE_PATH={tmp_path / "online-evidence.json"} \
AIRGAP_EVIDENCE_PATH={airgap_evidence} \
BUNDLE_PATH={tmp_path / "bundle.json"} \
SUMMARY_PATH={summary} \
HUB_URL=http://hub:8080 \
ONLINE_EDGE_URL=http://online:8080 \
AIRGAP_EDGE_URL=http://airgap:8080 \
ONLINE_DEVICE_ID=edge-online \
AIRGAP_DEVICE_ID=edge-airgap \
ONLINE_DEVICE_PROFILE=x86_64-cpu \
AIRGAP_DEVICE_PROFILE=rpi5-tflite \
ONLINE_PACKAGE_ID=pkg-online \
AIRGAP_PACKAGE_ID=pkg-airgap \
ONLINE_ROLLOUT_ID=rollout-online \
AIRGAP_ROLLOUT_ID=rollout-airgap \
write_summary connected-lab >/dev/null
"""
        subprocess.run(["bash", "-lc", script], check=True)

        payload = json.loads(summary.read_text(encoding="utf-8"))
        assert payload["schema_version"] == "temms-mvp-acceptance/v1"
        assert payload["mode"] == "connected-lab"
        assert payload["edges"]["online"]["state"] == "rolled_back"
        assert payload["edges"]["airgap"]["state"] == "activated"
        assert payload["checks"] == [
            {
                "name": "online_rollout_state",
                "actual": "rolled_back",
                "expected": "rolled_back",
                "passed": True,
            },
            {
                "name": "airgap_rollout_state",
                "actual": "activated",
                "expected": "activated",
                "passed": True,
            },
        ]
        assert payload["result"] == "passed"
        assert payload["artifacts"]["central_deployment_status"] == str(status)
