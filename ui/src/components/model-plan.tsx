import { Activity, Cpu, PackageCheck, UploadCloud } from "lucide-react";
import type { FormEvent, Ref } from "react";
import { compactDate } from "../lib/hub-format";
import {
  formatBenchmark,
  formatBenchmarkFreshness,
  formatBenchmarkTarget,
  formatPerformanceSlo,
  formatResourceEnvelope,
  providerDisplayForModel
} from "../lib/runtime-fit";
import type {
  EdgeRuntimeFit,
  ModelRecord,
  RuntimeFitDisplay
} from "../lib/workbench-types";
import type { RuntimeTarget, RuntimeValidation } from "../types";
import { EmptyState } from "./deploy-lists";
import { Badge, Button, Submit } from "./ui";

export function ModelPlanStage({
  assetsOpen,
  assetsRef,
  assetsSectionClassName,
  modelRef,
  models,
  nextPackageState,
  resourceEnvelopeFit,
  runtimeFitDisplay,
  selectedModel,
  selectedModelSectionClassName,
  selectedRuntime,
  selectedRuntimeValidation,
  onGoRuntime,
  onPromoteSelectedPackage,
  onSelectModel,
  onSubmitForm
}: {
  assetsOpen: boolean;
  assetsRef: Ref<HTMLDetailsElement>;
  assetsSectionClassName: string;
  modelRef: Ref<HTMLElement>;
  models: ModelRecord[];
  nextPackageState: string | undefined;
  resourceEnvelopeFit: EdgeRuntimeFit;
  runtimeFitDisplay: RuntimeFitDisplay;
  selectedModel: ModelRecord | undefined;
  selectedModelSectionClassName: string;
  selectedRuntime: RuntimeTarget | undefined;
  selectedRuntimeValidation: RuntimeValidation | undefined;
  onGoRuntime: () => void;
  onPromoteSelectedPackage: () => void;
  onSelectModel: (modelId: string) => void;
  onSubmitForm: (name: string, event: FormEvent<HTMLFormElement>) => void;
}): JSX.Element {
  return (
    <>
      <section
        className="section section-primary model-inventory-section"
        aria-labelledby="models-heading"
        data-testid="model-plan-inventory"
      >
        <div className="section-header">
          <div>
            <span className="section-kicker">Mission model plan</span>
            <h2 id="models-heading">Select the model that will ship to the edge</h2>
          </div>
          <Badge value={selectedModel?.packagePromotion ?? "no models"} />
        </div>

        <div className="model-list">
          {models.length ? (
            models.map((model) => (
              <button
                className={model.id === selectedModel?.id ? "model-row model-row-active" : "model-row"}
                key={`${model.packageId}-${model.id}`}
                type="button"
                onClick={() => onSelectModel(model.id)}
              >
                <span className="model-main">
                  <strong>{model.name}</strong>
                  <small>{model.id}</small>
                </span>
                <span>{model.format}</span>
                <span>{model.profiles.join(", ") || "any profile"}</span>
                <span>{formatBenchmark(model)}</span>
                <Badge value={model.signed ? "signed" : "unsigned"} />
              </button>
            ))
          ) : (
            <EmptyState title="No models registered" detail="Register a signed TEMMS package to populate the model inventory." />
          )}
        </div>
      </section>

      <section
        className={selectedModelSectionClassName}
        aria-labelledby="selected-model-heading"
        data-testid="model-plan-decision"
        ref={modelRef}
        tabIndex={-1}
      >
        <div className="section-header">
          <div>
            <span className="section-kicker">Model decision</span>
            <h2 id="selected-model-heading">{selectedModel?.name ?? "No model selected"}</h2>
          </div>
          {selectedModel ? <Badge value={selectedModel.packagePromotion} /> : null}
        </div>
        {selectedModel ? (
          <div className="facts">
            <Fact label="Package" value={selectedModel.packageId} />
            <Fact label="Version" value={`${selectedModel.packageName} ${selectedModel.packageVersion}`} />
            <Fact label="Runtime" value={selectedModel.runtimes.join(", ") || "not declared"} />
            <Fact label="Provider" value={providerDisplayForModel(selectedModel, selectedRuntime)} />
            <Fact label="Performance SLO" value={formatPerformanceSlo(selectedModel)} />
            <Fact label="Resource envelope" value={formatResourceEnvelope(selectedModel)} />
            <Fact label="Benchmark" value={formatBenchmark(selectedModel)} />
            <Fact label="Benchmark age" value={formatBenchmarkFreshness(selectedModel)} />
            <Fact label="Tested on" value={formatBenchmarkTarget(selectedModel)} />
            <Fact label="Validation" value={selectedRuntimeValidation ? "passed runtime check" : "not validated"} />
            <Fact label="Runtime fit" value={`${runtimeFitDisplay.label}: ${runtimeFitDisplay.detail}`} />
            <Fact label="Resource fit" value={`${resourceEnvelopeFit.label}: ${resourceEnvelopeFit.detail}`} />
            <Fact label="Source" value={selectedModel.source} />
            <Fact label="Updated" value={compactDate(selectedModel.updatedAt)} />
          </div>
        ) : null}
        <div className="button-row">
          <Button
            icon={<PackageCheck size={16} />}
            variant="secondary"
            disabled={!nextPackageState}
            onClick={onPromoteSelectedPackage}
          >
            {nextPackageState ? `Promote to ${nextPackageState}` : "Released"}
          </Button>
          <Button
            icon={<Cpu size={16} />}
            disabled={!selectedModel}
            onClick={onGoRuntime}
          >
            Continue to Runtime Fit
          </Button>
        </div>
      </section>

      <details
        className={assetsSectionClassName}
        aria-labelledby="ingest-heading"
        data-testid="model-plan-advanced-intake"
        ref={assetsRef}
        tabIndex={-1}
        open={assetsOpen}
      >
        <summary className="section-header">
          <div>
            <span className="section-kicker">Advanced intake</span>
            <h2 id="ingest-heading">Register packages, enroll edge nodes, or import bundles</h2>
          </div>
          <Badge value="setup" />
        </summary>
        <div className="utility-grid">
          <form className="stack" onSubmit={(event) => onSubmitForm("register-package", event)}>
            <label className="field">
              <span>Signed package path</span>
              <input name="package_path" placeholder="/path/to/package" required />
            </label>
            <input name="actor" type="hidden" value="operator:mission-package-workbench" />
            <label className="check">
              <input name="strict_metadata" type="checkbox" defaultChecked />
              <span>Strict metadata</span>
            </label>
            <Submit icon={<PackageCheck size={16} />}>Register package</Submit>
          </form>
          <form className="stack" onSubmit={(event) => onSubmitForm("enroll-device", event)}>
            <label className="field">
              <span>Device ID</span>
              <input name="device_id" defaultValue="edge-demo" required />
            </label>
            <label className="field">
              <span>Profile</span>
              <input name="profile" defaultValue="x86_64-cpu" required />
            </label>
            <label className="field">
              <span>Site</span>
              <input name="site" defaultValue="local-lab" />
            </label>
            <Submit icon={<Activity size={16} />}>Enroll edge</Submit>
          </form>
          <form className="stack" onSubmit={(event) => onSubmitForm("airgap-import", event)}>
            <label className="field">
              <span>Air-gap bundle JSON</span>
              <textarea name="bundle" rows={7} placeholder='{"schema_version":"temms-hub-lite-bundle/v1"}' />
            </label>
            <Submit icon={<UploadCloud size={16} />} variant="secondary">
              Import bundle
            </Submit>
          </form>
        </div>
      </details>
    </>
  );
}

function Fact({ label, value }: { label: string; value: string }): JSX.Element {
  return (
    <div className="fact">
      <span>{label}</span>
      <strong>{value || "-"}</strong>
    </div>
  );
}
